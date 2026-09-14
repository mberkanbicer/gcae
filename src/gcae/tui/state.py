"""Presentation state for the dashboard.

The reducer turns runtime events into *display* data.  It never owns runtime truth:
``runtime.state`` stays authoritative and is read on demand.  Only presentation facts
live here (which tool is running, when the last rollback happened, timeline rows).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ..models import Event
from . import formatters

PANELS = (
    "status",
    "objective",
    "plan",
    "activity",
    "checkpoint",
    "evaluation",
    "validation",
    "metrics",
    "timeline",
    "banner",
    "footer",
)

LOG_HISTORY = 2000
TIMELINE_HISTORY = 400
ROLLBACK_EMPHASIS = timedelta(seconds=20)


@dataclass
class TimelineRow:
    at: datetime
    icon: str
    text: str
    style: str


@dataclass
class ActionView:
    """What the agent is doing right now, from real event timing.

    ``label`` is the human action ("Write src/app.py"), ``target`` the path or scope it
    acts on and ``lines`` any extra detail worth one row.  Raw tool names and raw
    arguments never reach the panel: formatting happens in ``formatters``.
    """

    label: str
    lines: list[str] = field(default_factory=list)
    target: str = ""
    #: "tool" (a worktree operation), "model" (a model call in flight) or "runtime"
    #: (rollback, recovery, verification, a question).  The panel presents each differently.
    kind: str = "tool"
    state: str = "waiting"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    duration_ms: float | None = None
    expected: str = ""
    detail: str = ""


@dataclass
class UiState:
    request: str | None = None
    last_error: str | None = None
    agent_done: bool = False
    started_at: datetime | None = None
    phase: str = "analyze"
    plan: list[dict[str, Any]] = field(default_factory=list)
    plan_reason: str = ""
    completed_steps: int = 0
    current_step: dict[str, Any] | None = None
    action: ActionView | None = None
    candidate: dict[str, Any] | None = None
    checkpoint: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    evaluation: dict[str, Any] | None = None
    verification: dict[str, Any] | None = None
    context: dict[str, Any] = field(default_factory=dict)
    memory: dict[str, int] = field(default_factory=dict)
    # live streamed-model progress: characters, reasoning characters, elapsed, preview
    stream: dict[str, Any] | None = None
    # which role the current provider call belongs to (controller, planner, evaluator…)
    provider_role: str = ""
    # the step whose speculative changes were already announced in the timeline
    _candidate_announced: str | None = None
    #: the goal the run was last working on (the objective box keeps it after completion)
    last_goal: str = ""
    rollback: dict[str, Any] | None = None
    # bookkeeping losses (state/memory/events): visible for the rest of the run, not just an event
    degradations: list[str] = field(default_factory=list)
    rollback_at: datetime | None = None
    rollbacks: int = 0
    replans: int = 0
    work_files: list[str] = field(default_factory=list)
    timeline: list[TimelineRow] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ seeding

    def seed_from_state(self, state: Any) -> None:
        """Adopt persisted runtime truth that no event will re-announce (resume)."""
        self.request = state.objective
        self.started_at = state.created_at
        self.phase = state.phase.value
        self.completed_steps = state.accepted_steps
        self.plan = [
            {"id": step.id, "goal": step.goal, "status": step.status} for step in state.plan
        ]
        if state.latest_validation is not None:
            self.validation = state.latest_validation.model_dump(mode="json")
        if state.last_verification is not None:
            self.verification = state.last_verification.model_dump(mode="json")
        if state.accepted_commit:
            self.checkpoint = {"commit": state.accepted_commit, "message": "", "kind": "resumed"}
        if state.status == "complete":
            self.agent_done = True
        active = next((step for step in self.plan if step["status"] == "active"), None)
        if active is not None:
            self.current_step = {"id": active["id"], "goal": active["goal"]}
        if (
            state.latest_user_instruction
            and state.latest_user_instruction != state.original_request
        ):
            self.request = state.objective

    # ------------------------------------------------------------------ queries

    @property
    def role(self) -> str:
        if self.phase in {"complete", "failed"}:
            return ""
        return formatters.role_for_phase(self.phase)

    @property
    def goal_is_active(self) -> bool:
        """True only when a step is genuinely running right now."""
        return self.current_step is not None and not self.agent_done

    @property
    def current_goal(self) -> str:
        if self.current_step:
            return str(self.current_step.get("goal") or "")
        for step in self.plan:
            if step.get("status") in {"active", "pending"}:
                return str(step.get("goal") or "")
        return ""

    def plan_position(self) -> tuple[int, int]:
        """(accepted steps, accepted + open). Accepted steps leave the open plan."""
        open_steps = len(self.plan)
        return (self.completed_steps, self.completed_steps + open_steps)

    def action_elapsed(self, now: datetime | None = None) -> float:
        if self.action is None:
            return 0.0
        reference = now or datetime.now(UTC)
        return max(0.0, (reference - self.action.started_at).total_seconds())

    def rollback_active(self, now: datetime | None = None) -> bool:
        if self.rollback_at is None:
            return False
        reference = now or datetime.now(UTC)
        return reference - self.rollback_at < ROLLBACK_EMPHASIS

    # ------------------------------------------------------------------ reducer

    def apply(self, event: Event) -> set[str]:
        """Fold one event into the presentation state; return panels to re-render."""
        changed: set[str] = set()
        if event.phase is not None:
            self.phase = event.phase.value
        payload = event.payload or {}
        if event.event_type.startswith("phase:"):
            return {"status"}

        phase = event.phase.value if event.phase else None
        log = formatters.log_line(event.event_type, phase, payload)
        self.logs.append(f"{event.timestamp.astimezone().strftime('%H:%M:%S')} {log}")
        self.logs = self.logs[-LOG_HISTORY:]
        changed.add("logs")

        succeeded = payload.get("success") if "success" in payload else None
        entry = formatters.timeline_entry(
            event.event_type, payload, succeeded=succeeded if isinstance(succeeded, bool) else None
        )
        if entry is not None:
            icon, text, style = entry
            self._add_timeline(event.timestamp, icon, text, style)
            changed.add("timeline")

        handler = getattr(self, f"_on_{event.event_type}", None)
        if handler is not None:
            changed |= handler(event, payload)
        elif event.event_type == "run_started":
            self.started_at = event.timestamp
            changed |= {"status", "objective"}
        elif event.event_type == "run_resumed":
            self.started_at = event.timestamp
            changed |= {"status", "objective"}
        elif event.event_type == "run_completed":
            self.agent_done = True
            self.current_step = None
            changed |= {"status", "banner", "footer", "objective", "activity"}
        elif event.event_type == "run_failed":
            self.agent_done = True
            self.current_step = None
            self.last_error = str(payload.get("reason") or "run failed")
            changed |= {"status", "banner", "footer", "activity", "objective"}
        elif event.event_type == "run_stopped":
            self.agent_done = True
            self.current_step = None
            changed |= {"status", "banner", "footer", "activity", "objective"}
        return changed

    def add_note(self, icon: str, text: str, style: str = "muted") -> None:
        """Add a UI-side note (task accepted, instruction queued) to the timeline."""
        self._add_timeline(datetime.now(UTC), icon, text, style)

    def _add_timeline(self, at: datetime, icon: str, text: str, style: str) -> None:
        self.timeline.append(TimelineRow(at=at, icon=icon, text=text, style=style))
        self.timeline = self.timeline[-TIMELINE_HISTORY:]

    # ------------------------------------------------------------------ handlers

    def _on_plan_updated(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.plan = list(payload.get("steps") or [])
        self.plan_reason = str(payload.get("reason") or "")
        self.completed_steps = int(payload.get("completed") or 0)
        if payload.get("replaced"):
            self.replans += 1
            self.rollback = None
        return {"plan", "objective", "metrics"}

    def _on_step_started(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self._candidate_announced = None
        self.last_goal = str(payload.get("goal") or self.last_goal)
        self.current_step = {
            "id": event.step_id,
            "goal": payload.get("goal"),
            "index": payload.get("index"),
            "total": payload.get("total"),
        }
        for step in self.plan:
            if step.get("id") == event.step_id:
                step["status"] = "active"
        self.action = ActionView(
            label="model",
            lines=["choosing the next action"],
            kind="model",
            state="waiting",
            started_at=event.timestamp,
        )
        return {"plan", "activity", "objective"}

    def _on_decision(self, event: Event, payload: dict[str, Any]) -> set[str]:
        tool = payload.get("tool")
        expected = str(payload.get("expected_result") or "")
        reason = str(payload.get("reason_summary") or "")
        if isinstance(tool, dict) and tool.get("name"):
            action_label, target = formatters.tool_presentation(
                str(tool.get("name")), tool.get("arguments") or {}
            )
            self.action = ActionView(
                label=action_label,
                lines=[reason] if reason else [],
                target=target,
                kind="tool",
                state="running",
                started_at=event.timestamp,
                expected=expected or reason,
            )
        else:
            self.action = ActionView(
                label=str(payload.get("action") or "decide"),
                lines=[reason] if reason else [],
                state="running",
                started_at=event.timestamp,
                expected=expected,
            )
        return {"activity"}

    def _on_tool_result(self, event: Event, payload: dict[str, Any]) -> set[str]:
        """Finish the current action: keep its human label, add the shortest honest outcome.

        The tool's raw name never replaces the action's wording ("Read src/parser.py" must
        not turn back into ``read_file`` when it completes).
        """
        previous = self.action
        success = bool(payload.get("success"))
        detail = ""
        if success:
            detail = _first_line(payload.get("output"), 44)
        else:
            if payload.get("exit_code") is not None:
                detail = f"exit {payload['exit_code']}"
            message = _first_line(payload.get("error") or payload.get("output"), 60)
            if message:
                detail = f"{detail} · {message}" if detail else message
        label = previous.label if previous and previous.kind == "tool" else ""
        if not label:
            label, _ = formatters.tool_presentation(
                str(payload.get("tool") or "tool"), payload.get("arguments") or {}
            )
        self.action = ActionView(
            label=label,
            lines=previous.lines if previous else [],
            target=previous.target if previous else "",
            kind=previous.kind if previous else "tool",
            state="done" if success else "failed",
            started_at=previous.started_at if previous else event.timestamp,
            duration_ms=payload.get("duration_ms"),
            expected=previous.expected if previous else "",
            detail=detail,
        )
        return {"activity"}

    def _on_candidate_state(self, event: Event, payload: dict[str, Any]) -> set[str]:
        """Track speculative work; announce it once per step instead of per tool call.

        ``candidate_state`` fires after every tool call, so rendering each one would bury
        the semantic story.  The panel always shows the live scope; the timeline gets a
        single ``+ candidate changed`` row per step, when speculative work first appears.
        """
        self.candidate = payload
        step_id = self.current_step.get("id") if self.current_step else None
        if payload.get("dirty") and step_id != self._candidate_announced:
            self._candidate_announced = step_id
            self._add_timeline(
                event.timestamp,
                "+",
                f"candidate changed · {formatters.snapshot_summary(payload)}",
                "warning",
            )
            return {"checkpoint", "timeline"}
        return {"checkpoint"}

    def _on_checkpoint_created(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.checkpoint = payload
        self.rollback = None
        return {"checkpoint"}

    def _on_step_accepted(self, event: Event, payload: dict[str, Any]) -> set[str]:
        step_id = event.step_id
        for path in payload.get("changed_files") or []:
            if path not in self.work_files:
                self.work_files.append(str(path))
        self.plan = [step for step in self.plan if step.get("id") != step_id]
        self.completed_steps = int(payload.get("accepted_steps") or self.completed_steps)
        self.current_step = None
        return {"plan", "objective", "checkpoint", "metrics"}

    def _on_validation(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.validation = payload
        return {"validation", "activity"}

    def _on_evaluation(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.evaluation = payload
        decision = str(payload.get("decision"))
        if decision == "rollback":
            self.rollbacks += 1
        if decision == "replan":
            self.replans += 1
        return {"evaluation", "validation", "banner", "status", "objective"}

    def _on_rollback_completed(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.rollback = payload
        self.rollback_at = event.timestamp
        self.action = ActionView(
            label="rollback",
            lines=[str(payload.get("reason") or "")],
            kind="runtime",
            state="done",
            started_at=event.timestamp,
        )
        return {"checkpoint", "activity", "banner", "status"}

    def _on_verification_started(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.action = ActionView(
            label="verification",
            lines=["checking success criteria"],
            kind="runtime",
            state="running",
            started_at=event.timestamp,
        )
        return {"activity"}

    def _on_verification_completed(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.verification = payload
        self.action = ActionView(
            label="verification",
            lines=[],
            kind="runtime",
            state="done" if payload.get("passed") else "failed",
            started_at=self.action.started_at if self.action else event.timestamp,
        )
        return {"validation", "activity"}

    def _on_context_built(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.context = payload
        return {"metrics"}

    def _on_memory_updated(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.memory = dict(payload.get("counts") or {})
        return {"metrics"}

    def _on_model_escalated(self, event: Event, payload: dict[str, Any]) -> set[str]:
        return {"status"}

    def _on_provider_started(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.provider_role = str(payload.get("role") or "")
        self.action = ActionView(
            label=str(payload.get("role") or "model"),
            lines=[str(payload.get("model") or "")],
            kind="model",
            state="waiting",
            started_at=event.timestamp,
        )
        self.stream = None
        return {"activity", "metrics"}

    def _on_provider_first_token(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.stream = dict(payload)
        if self.action is not None:
            self.action.state = "running"
        return {"activity", "metrics"}

    def _on_provider_progress(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.stream = dict(payload)
        if self.action is not None:
            self.action.state = "running"
        return {"activity", "metrics"}

    def _on_provider_waiting(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.stream = {**dict(payload), "waiting": True}
        if self.action is not None:
            self.action.state = "waiting"
        return {"activity", "metrics"}

    def _on_runtime_degraded(self, event: Event, payload: dict[str, Any]) -> set[str]:
        component = str(payload.get("component") or "runtime")
        if not any(item.startswith(component) for item in self.degradations):
            self.degradations.append(f"{component}: {payload.get('error') or 'failed'}")
        return {"status", "timeline"}

    def _on_model_failover(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.action = ActionView(
            label="failover",
            lines=[f"{payload.get('role')} → {payload.get('model')}"],
            kind="runtime",
            state="waiting",
            detail=str(payload.get("reason") or ""),
        )
        return {"activity", "status"}

    def _on_recovery_started(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.action = ActionView(
            label="self-diagnosis",
            lines=[str(payload.get("trigger") or "")],
            kind="runtime",
            state="running",
            started_at=event.timestamp,
        )
        return {"activity"}

    def _on_recovery_completed(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.action = ActionView(
            label="recovery",
            lines=[
                str(payload.get("root_cause") or ""),
                f"next: {payload.get('corrective_instruction') or ''}",
            ],
            kind="runtime",
            state="recovered",
            started_at=event.timestamp,
        )
        return {"activity"}

    def _on_user_override(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.request = str(payload.get("text") or self.request or "")
        return {"objective", "status"}

    def _on_user_question(self, event: Event, payload: dict[str, Any]) -> set[str]:
        self.action = ActionView(
            label="question",
            lines=[str(payload.get("question") or "")],
            kind="runtime",
            state="waiting",
            started_at=event.timestamp,
        )
        return {"activity", "banner"}

def _first_line(value: Any, width: int) -> str:
    """First non-empty line of a tool's output, elided: one row, never a dump."""
    for line in str(value or "").splitlines():
        text = " ".join(line.split())
        if text:
            return text[:width]
    return ""
