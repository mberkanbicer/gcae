"""Semantic progress events: the presentation layer between runtime and UI.

Three levels, never mixed:

- Level 1 (main progress) renders :class:`ProgressEvent` — one line per meaningful
  engineering action with an outcome. This is what most users watch.
- Level 2 (detail) expands one progress event's ``detail`` payload: command, files,
  output excerpts, evaluator reason, evidence, recovery strategy.
- Level 3 (raw logs) keeps every runtime event, including model streaming telemetry.

A ``ProgressEvent`` is presentation data derived from runtime events. It duplicates no
authoritative state: the runtime owns truth, the feed owns wording.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ProgressCategory(StrEnum):
    """Small useful set of engineering activities. Not a taxonomy project."""

    PLAN = "plan"
    INSPECT = "inspect"
    EDIT = "edit"
    EXECUTE = "execute"
    TEST = "test"
    VALIDATE = "validate"
    OBSERVE = "observe"
    FAILURE = "failure"
    DIAGNOSE = "diagnose"
    RECOVER = "recover"
    ROLLBACK = "rollback"
    REPLAN = "replan"
    ACCEPT = "accept"
    VERIFY = "verify"
    USER = "user"
    SYSTEM = "system"


class ProgressStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    FAIL = "fail"
    INFO = "info"


@dataclass
class ProgressEvent:
    """One line of operational progress with an expandable detail payload."""

    id: str
    timestamp: datetime
    category: ProgressCategory
    phase: str = ""
    title: str = ""
    summary: str = ""
    result: str = ""
    status: ProgressStatus = ProgressStatus.INFO
    trajectory_step_id: str = ""
    related_file: str = ""
    related_command: str = ""
    evidence_ids: list[int] = field(default_factory=list)
    recovery_id: str = ""
    expandable: bool = False
    severity: str = "info"
    detail: dict[str, Any] = field(default_factory=dict)


#: tools that only look at the tree: consecutive runs group into one INSPECT line
_INSPECT_TOOLS = frozenset({"read_file", "list_files", "search_text"})
#: tools that change the tree
_EDIT_TOOLS = frozenset({"write_file", "create_file", "apply_patch"})


def _short(text: object, limit: int = 90) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def summarize_event(
    event_type: str,
    payload: dict[str, Any],
    phase: str = "",
    step_id: str = "",
    event_id: str = "",
    timestamp: datetime | None = None,
) -> ProgressEvent | None:
    """Derive one progress line from a runtime event, or None when it is not progress.

    Model streaming telemetry (characters, reasoning counts, chunks, heartbeats) never
    becomes progress: it stays in the raw logs.
    """
    at = timestamp or datetime.now(UTC)

    def make(
        category: ProgressCategory,
        title: str,
        summary: str = "",
        status: ProgressStatus = ProgressStatus.INFO,
        related_file: str = "",
        related_command: str = "",
        evidence_ids: list[int] | None = None,
        severity: str = "info",
        detail: dict[str, object] | None = None,
    ) -> ProgressEvent:
        return ProgressEvent(
            id=event_id or f"{event_type}-{at.isoformat()}",
            timestamp=at,
            category=category,
            phase=phase,
            title=title,
            summary=summary,
            status=status,
            trajectory_step_id=step_id,
            related_file=related_file,
            related_command=related_command,
            evidence_ids=evidence_ids or [],
            severity=severity,
            detail=detail or {},
        )

    if event_type in {
        "provider_started",
        "provider_first_token",
        "provider_progress",
        "provider_waiting",
        "provider_finished",
        "context_built",
        "candidate_state",
        "memory_updated",
    }:
        return None
    if event_type.startswith("phase:"):
        return None
    if event_type == "run_started":
        return make(
            ProgressCategory.PLAN,
            "Run started",
            _short(payload.get("objective", ""), 80),
        )
    if event_type == "run_resumed":
        return make(ProgressCategory.SYSTEM, "Run resumed")
    if event_type == "plan_updated":
        reason = str(payload.get("reason") or "plan updated")
        diff = payload.get("diff") if isinstance(payload.get("diff"), dict) else {}
        scope = ""
        if diff:
            kept = len(diff.get("preserved") or [])
            changed = len(diff.get("replaced") or []) + len(diff.get("invalidated") or [])
            added = len(diff.get("inserted") or [])
            scope = f" · preserved {kept} · replaced {changed} · added {added}"
        version = payload.get("version")
        prefix = f"v{version} " if isinstance(version, int) and version > 1 else ""
        if reason == "initial plan":
            steps = payload.get("steps") or []
            return make(
                ProgressCategory.PLAN, f"Plan created · {len(steps)} steps", _short(reason)
            )
        return make(
            ProgressCategory.REPLAN if payload.get("replaced") else ProgressCategory.PLAN,
            f"{prefix}{_short(reason, 80)}{scope}",
            status=ProgressStatus.INFO,
        )
    if event_type == "step_started":
        goal = _short(payload.get("goal", ""), 80)
        index, total = payload.get("index"), payload.get("total")
        prefix = f"Step {index}/{total} · " if index and total else ""
        return make(ProgressCategory.PLAN, f"{prefix}{goal}")
    if event_type == "decision":
        tool = payload.get("tool")
        if not isinstance(tool, dict) or not tool.get("name"):
            action = str(payload.get("action") or "decide")
            if action == "ask_user":
                return make(
                    ProgressCategory.USER,
                    "Asking the user",
                    _short(payload.get("reason_summary", ""), 80),
                )
            return None
        name = str(tool.get("name"))
        arguments = tool.get("arguments") or {}
        reason = _short(payload.get("reason_summary", ""), 80)
        if name in _INSPECT_TOOLS:
            path = str(arguments.get("path") or arguments.get("query") or "")
            return make(
                ProgressCategory.INSPECT,
                reason or f"Inspecting {path}",
                "",
                ProgressStatus.RUNNING,
                related_file=path,
                detail={"tool": name, "arguments": arguments},
            )
        if name in _EDIT_TOOLS:
            path = str(arguments.get("path") or "")
            return make(
                ProgressCategory.EDIT,
                reason or f"Updating {path}",
                "",
                ProgressStatus.RUNNING,
                related_file=path,
                detail={"tool": name, "arguments": arguments},
            )
        if name == "run_tests":
            return make(
                ProgressCategory.TEST,
                reason or "Running project tests",
                "",
                ProgressStatus.RUNNING,
                detail={"tool": name},
            )
        if name == "run_command":
            command = str(arguments.get("command") or "")
            return make(
                ProgressCategory.EXECUTE,
                reason or _short(command, 80),
                "",
                ProgressStatus.RUNNING,
                related_command=command,
                detail={"tool": name, "arguments": arguments},
            )
        return make(
            ProgressCategory.EXECUTE,
            reason or name,
            "",
            ProgressStatus.RUNNING,
            detail={"tool": name, "arguments": arguments},
        )
    if event_type == "context_warning":
        share = payload.get("memory_share")
        title = (
            f"Retrieved memory {float(share):.0%} of context"
            if isinstance(share, (int, float))
            else "Context anomaly"
        )
        dropped = payload.get("dropped_duplicates") or 0
        if dropped:
            title += f" · {int(dropped)} duplicates dropped"
        return make(ProgressCategory.SYSTEM, title, severity="warning")
    if event_type == "validation":
        if payload.get("passed"):
            commands = payload.get("command_results") or []
            return make(
                ProgressCategory.VALIDATE,
                f"Validation passed · {len(commands)} checks",
                status=ProgressStatus.OK,
            )
        failed = [
            str(item.get("tool") or "check")
            for item in (payload.get("command_results") or [])
            if not item.get("success")
        ]
        return make(
            ProgressCategory.FAILURE,
            f"Validation failed · {', '.join(failed[:3]) or 'diff check or scope'}",
            status=ProgressStatus.FAIL,
            severity="error",
            detail={"validation": payload},
        )
    if event_type == "evaluation":
        decision = str(payload.get("decision"))
        reason = _short(payload.get("reason") or "", 80)
        if decision == "accept":
            return make(ProgressCategory.ACCEPT, reason, status=ProgressStatus.OK)
        if decision == "rollback":
            return None  # rollback_completed carries the richer line
        if decision == "replan":
            return make(ProgressCategory.REPLAN, reason)
        if decision == "repair":
            return make(ProgressCategory.RECOVER, f"Repair · {reason}")
        if decision == "finish_candidate":
            return make(ProgressCategory.VERIFY, f"Finishing · {reason}")
        return None  # routine "continue" is not progress
    if event_type == "trajectory_step_completed":
        status = str(payload.get("status"))
        goal = _short(payload.get("semantic_goal") or "", 70)
        reason = _short(payload.get("decision_reason") or "", 80)
        if status == "accepted":
            return make(
                ProgressCategory.ACCEPT,
                f"Checkpoint · {goal}",
                status=ProgressStatus.OK,
            )
        if status == "rejected":
            return make(
                ProgressCategory.FAILURE,
                reason or f"Candidate rejected · {goal}",
                status=ProgressStatus.FAIL,
                severity="error",
                detail={"decision_reason": reason, "goal": goal},
            )
        if status == "repaired":
            return make(ProgressCategory.RECOVER, f"Repairing · {reason or goal}")
        if status == "replanned":
            return make(ProgressCategory.REPLAN, reason or goal)
        if status == "blocked":
            return make(
                ProgressCategory.FAILURE,
                reason or goal,
                status=ProgressStatus.FAIL,
                severity="error",
            )
        return None
    if event_type == "evidence_recorded":
        if payload.get("contradicts"):
            claim = _short(payload.get("claim") or "the expectation", 70)
            return make(
                ProgressCategory.FAILURE,
                f"Evidence contradicts · {claim}",
                _short(payload.get("summary") or "", 80),
                status=ProgressStatus.FAIL,
                severity="warning",
                evidence_ids=(
                    [int(payload["id"])] if isinstance(payload.get("id"), int) else []
                ),
                detail={"summary": payload.get("summary"), "claim": claim},
            )
        return None
    if event_type in {"failure_classified", "strategy_ineffective"}:
        lesson = _short(payload.get("lesson") or payload.get("tool") or "", 80)
        return make(
            ProgressCategory.DIAGNOSE,
            lesson,
            status=ProgressStatus.INFO,
            severity="warning",
            detail=dict(payload),
        )
    if event_type == "interactive_detected":
        return make(
            ProgressCategory.DIAGNOSE,
            f"Program needs input · {_short(payload.get('prompt') or '', 60)}",
            severity="warning",
        )
    if event_type == "rollback_completed":
        discarded = payload.get("discarded") or []
        detail = f"{len(discarded)} files discarded" if discarded else "speculative state discarded"
        reason = _short(payload.get("reason") or "", 70)
        return make(
            ProgressCategory.ROLLBACK,
            f"Restored {str(payload.get('to_commit') or '')[:7]} · {detail}",
            reason,
            detail={"discarded": discarded, "reason": reason},
        )
    if event_type in {"replan", "plan_updated"}:
        return make(ProgressCategory.REPLAN, _short(payload.get("reason", ""), 80))
    if event_type in {"recovery_started", "recovery_completed", "recovery_failed"}:
        if event_type == "recovery_started":
            return make(
                ProgressCategory.DIAGNOSE,
                f"Diagnosing · {_short(payload.get('trigger', ''), 70)}",
            )
        if event_type == "recovery_completed":
            cause = _short(payload.get("root_cause", ""), 60)
            fix = _short(payload.get("corrective_instruction", ""), 60)
            return make(
                ProgressCategory.RECOVER,
                f"{cause} → {fix}" if fix else cause,
                status=ProgressStatus.OK,
                detail=dict(payload),
            )
        return make(
            ProgressCategory.DIAGNOSE,
            f"Self-diagnosis unavailable · {_short(payload.get('error', ''), 60)}",
            severity="warning",
        )
    if event_type in {"model_failover", "model_escalated"}:
        return make(
            ProgressCategory.RECOVER,
            _short(
                payload.get("reason") or f"Switched model · {payload.get('model', '')}", 80
            ),
            detail=dict(payload),
        )
    if event_type == "step_accepted":
        files = payload.get("changed_files") or []
        detail = f"{len(files)} files" if files else "no file changes"
        return make(
            ProgressCategory.ACCEPT,
            f"Accepted · {detail}",
            _short(payload.get("goal", ""), 60),
            status=ProgressStatus.OK,
        )
    if event_type == "checkpoint_created":
        return make(
            ProgressCategory.ACCEPT,
            f"Checkpoint {str(payload.get('commit') or '')[:7]}",
            _short(payload.get("message", ""), 60),
            status=ProgressStatus.OK,
        )
    if event_type in {"verification_started", "verification_completed"}:
        if event_type == "verification_started":
            return make(ProgressCategory.VERIFY, "Final verification started")
        if payload.get("passed") and payload.get("hygiene_passed", True):
            criteria = payload.get("criteria") or []
            return make(
                ProgressCategory.VERIFY,
                f"Verification passed · {len(criteria)} criteria",
                status=ProgressStatus.OK,
            )
        missing = payload.get("missing_requirements") or []
        detail = _short(", ".join(str(item) for item in missing[:2]), 70)
        return make(
            ProgressCategory.VERIFY,
            f"Verification failed · {detail or 'hygiene failed'}",
            status=ProgressStatus.FAIL,
            severity="error",
        )
    if event_type == "guardian_check":
        action = str(payload.get("recovery_action") or "")
        kind = str(payload.get("kind") or "")
        evidence = _short(payload.get("evidence") or "", 70)
        subject = str(payload.get("subject") or "runtime")
        if action in {"wait_for_user", "mark_blocked", "mark_fatal"}:
            return make(
                ProgressCategory.FAILURE if action == "mark_fatal" else ProgressCategory.USER,
                f"{subject}: {kind}",
                evidence,
                status=ProgressStatus.FAIL if action == "mark_fatal" else ProgressStatus.INFO,
                severity="error" if action == "mark_fatal" else "warning",
                detail=dict(payload),
            )
        return make(
            ProgressCategory.RECOVER if action else ProgressCategory.DIAGNOSE,
            f"{subject}: {kind}" if kind else subject,
            f"{evidence} → {action}" if action and evidence else evidence or action,
            detail=dict(payload),
        )
    if event_type == "health_changed":
        to_state = str(payload.get("to") or "")
        reason = _short(payload.get("reason") or "", 70)
        if to_state in {"healthy", "degraded"}:
            return None
        return make(
            ProgressCategory.SYSTEM,
            f"Health: {to_state}",
            reason,
            severity="error" if to_state == "fatal" else "warning",
        )
    if event_type in {"user_question", "user_input_supplied", "user_override"}:
        text = _short(
            payload.get("question") or payload.get("text") or "user input", 80
        )
        return make(ProgressCategory.USER, text)
    if event_type in {"run_blocked", "run_failed", "run_completed", "run_stopped"}:
        if event_type == "run_completed":
            return make(
                ProgressCategory.VERIFY, "Run completed", status=ProgressStatus.OK
            )
        reason = _short(payload.get("reason") or event_type, 80)
        return make(
            ProgressCategory.FAILURE if event_type == "run_failed" else ProgressCategory.USER,
            reason,
            status=ProgressStatus.FAIL if event_type == "run_failed" else ProgressStatus.INFO,
            severity="error" if event_type == "run_failed" else "warning",
        )
    if event_type in {"execution_evidence_required", "repetition_detected",
                      "step_budget_exhausted", "stagnation_detected"}:
        return make(
            ProgressCategory.DIAGNOSE,
            _short(
                payload.get("reason") or payload.get("tool") or event_type.replace("_", " "),
                80,
            ),
            severity="warning",
        )
    if event_type in {
        "conflict_detected",
        "conflict_resolved",
        "conflict_unresolved",
        "merge_completed",
        "worktree_cleaned",
        "planner_fallback",
        "interactive_input_required",
        "repository_notice",
        "success_criteria_adopted",
    }:
        text = _short(
            payload.get("message") or payload.get("reason") or payload.get("prompt")
            or " · ".join(str(f) for f in (payload.get("files") or [])[:3])
            or event_type.replace("_", " "),
            80,
        )
        return make(
            ProgressCategory.SYSTEM
            if event_type in {"repository_notice", "worktree_cleaned", "success_criteria_adopted"}
            else ProgressCategory.USER
            if event_type == "interactive_input_required"
            else ProgressCategory.RECOVER
            if event_type in {"conflict_resolved", "merge_completed"}
            else ProgressCategory.FAILURE,
            text,
            severity="warning" if event_type == "conflict_unresolved" else "info",
        )
    return None


#: one icon per category for the progress timeline
CATEGORY_ICON: dict[ProgressCategory, str] = {
    ProgressCategory.PLAN: "●",
    ProgressCategory.INSPECT: "○",
    ProgressCategory.EDIT: "✎",
    ProgressCategory.EXECUTE: "▶",
    ProgressCategory.TEST: "✓",
    ProgressCategory.VALIDATE: "✓",
    ProgressCategory.OBSERVE: "·",
    ProgressCategory.FAILURE: "×",
    ProgressCategory.DIAGNOSE: "?",
    ProgressCategory.RECOVER: "↻",
    ProgressCategory.ROLLBACK: "↩",
    ProgressCategory.REPLAN: "↻",
    ProgressCategory.ACCEPT: "✓",
    ProgressCategory.VERIFY: "✓",
    ProgressCategory.USER: "i",
    ProgressCategory.SYSTEM: "·",
}

#: user-readable execution phase for the main UI (never a token stream)
PHASE_LABEL: dict[str, str] = {
    "plan": "PLANNING",
    "execute": "EXECUTING",
    "validate": "VALIDATING",
    "evaluate": "EVALUATING",
    "checkpoint": "CHECKPOINTING",
    "rollback": "ROLLING BACK",
    "verify": "VERIFYING",
    "complete": "DONE",
    "failed": "FAILED",
    "analyze": "STARTING",
}


def phase_label(phase: str | None, tool_name: str = "", mode: str = "") -> str:
    """What the agent is doing, as a user would describe it."""
    if tool_name == "run_tests":
        return "TESTING"
    if tool_name == "run_command":
        return "EXECUTING"
    if tool_name in {"read_file", "list_files", "search_text"}:
        return "INSPECTING"
    if tool_name in {"write_file", "create_file", "apply_patch"}:
        return "EDITING"
    if not phase:
        return "IDLE"
    return PHASE_LABEL.get(phase, phase.upper())


def suggest_next(event_type: str, payload: dict[str, Any]) -> str:
    """The explicit operational next step after an observation. Not chain-of-thought."""
    if event_type == "interactive_detected":
        return "Retry using scripted input or a PTY"
    if event_type == "strategy_ineffective":
        return "Change the method; the same approach already failed"
    if event_type == "execution_evidence_required":
        return "Run the code or its checks before declaring the step done"
    if event_type in {"recovery_started", "recovery_completed"}:
        fix = str(
            payload.get("corrective_instruction") or payload.get("trigger") or ""
        ).strip()
        return fix[:80] if fix else "Apply the diagnosed correction"
    if event_type in {"rollback_completed", "trajectory_step_completed"}:
        reason = str(payload.get("reason") or payload.get("decision_reason") or "").strip()
        return f"Rebuild from the trusted checkpoint: {reason[:60]}" if reason else ""
    if event_type == "evaluation" and str(payload.get("decision")) == "repair":
        return str(payload.get("reason") or "Fix the candidate in place")[:80]
    if event_type == "evaluation" and str(payload.get("decision")) == "replan":
        return str(payload.get("reason") or "Take a different path")[:80]
    if event_type == "failure_classified":
        return str(payload.get("lesson") or "")[:80]
    return ""


class ProgressFeed:
    """Ordered progress lines with grouping of low-level reads.

    Consecutive INSPECT tool events accumulate into one line ("Reviewed N relevant
    files"); any other progress flushes the group. Callers feed runtime events in order
    and render what is returned.
    """

    def __init__(self) -> None:
        self._events: list[ProgressEvent] = []
        self._pending_reads: list[ProgressEvent] = []
        self._counter = 0

    @property
    def events(self) -> list[ProgressEvent]:
        return list(self._events)

    def push(
        self,
        event_type: str,
        payload: dict[str, Any],
        phase: str = "",
        step_id: str = "",
        timestamp: datetime | None = None,
    ) -> list[ProgressEvent]:
        """Derive progress from one runtime event; returns new or updated lines.

        Unmapped events flush a pending read group only when they carry an outcome
        (a tool result ends the inspection it followed); streaming telemetry never
        splits a group."""
        summarized = summarize_event(
            event_type, payload, phase, step_id,
            event_id=f"p{self._counter}", timestamp=timestamp,
        )
        if summarized is None:
            if event_type == "tool_result":
                return self._flush_reads(timestamp)
            return []
        self._counter += 1
        tool = (summarized.detail.get("tool") or "") if summarized.detail else ""
        if summarized.category is ProgressCategory.INSPECT and tool in _INSPECT_TOOLS:
            self._pending_reads.append(summarized)
            return []
        flushed = self._flush_reads(timestamp)
        self._events.append(summarized)
        return [*flushed, summarized]

    def resolve(
        self,
        tool: str,
        step_id: str,
        success: bool,
        summary: str,
        timestamp: datetime | None = None,
    ) -> ProgressEvent | None:
        """Attach an outcome to the latest open EXECUTE/TEST/EDIT line for a step."""
        flushed = self._flush_reads(timestamp)
        for event in reversed(self._events):
            if (
                event.trajectory_step_id == step_id
                and event.category
                in {ProgressCategory.EXECUTE, ProgressCategory.TEST, ProgressCategory.EDIT}
                and event.status is ProgressStatus.RUNNING
                and (not event.related_command or tool in {"run_command", "run_tests"})
            ):
                event.status = ProgressStatus.OK if success else ProgressStatus.FAIL
                event.result = _short(summary, 90)
                event.severity = "info" if success else "error"
                return event
        return flushed[-1] if flushed else None

    def _flush_reads(self, timestamp: datetime | None) -> list[ProgressEvent]:
        if not self._pending_reads:
            return []
        reads = self._pending_reads
        self._pending_reads = []
        files = [event.related_file for event in reads if event.related_file]
        if len(reads) == 1:
            single = reads[0]
            single.title = single.title or f"Inspecting {files[0] if files else 'files'}"
            self._events.append(single)
            return [single]
        shown = ", ".join(files[:3])
        rest = f" +{len(files) - 3}" if len(files) > 3 else ""
        grouped = ProgressEvent(
            id=f"p{self._counter}",
            timestamp=timestamp or datetime.now(UTC),
            category=ProgressCategory.INSPECT,
            title=f"Reviewed {len(reads)} relevant files",
            summary=f"{shown}{rest}" if shown else "",
            status=ProgressStatus.OK,
            trajectory_step_id=reads[0].trajectory_step_id,
            expandable=True,
            detail={"files": files, "grouped_reads": True},
        )
        self._counter += 1
        self._events.append(grouped)
        return [grouped]

    def flush(self, timestamp: datetime | None = None) -> list[ProgressEvent]:
        """Emit any grouped reads still held (run end, step boundary)."""
        return self._flush_reads(timestamp)
