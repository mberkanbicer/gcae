"""Self-recovery: the runtime reads its own trace and tries to correct itself.

A run that is about to fail does not ask the user first. It builds a trace from its
*persisted* state — filtered events, validation and verification evidence, failure memories
— and asks a model for a structured diagnosis. The advisor sees the record of what was
tried rather than the run's working context, so it can question an assumption the
controller keeps repeating.

Everything here is bounded and fail-soft: a failed diagnosis never ends a run by itself,
and an unusable one leaves the caller's own ladder (ask the user, then fail) intact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum

from .models import AgentState, Diagnosis, Event, MemoryRecord
from .providers import Provider

# Event types worth diagnosing. Payload-heavy control-flow events (`decision`,
# `context_built`, `candidate_state`, `phase:*`) carry no failure signal of their own.
SIGNAL_EVENTS = frozenset(
    {
        "checkpoint_created",
        "conflict_detected",
        "conflict_resolved",
        "conflict_unresolved",
        "evaluation",
        "model_escalated",
        "planner_fallback",
        "recovery_completed",
        "recovery_failed",
        "recovery_started",
        "repetition_detected",
        "replan",
        "repository_notice",
        "rollback_completed",
        "run_failed",
        "step_accepted",
        "step_budget_exhausted",
        "step_started",
        "tool_result",
        "user_override",
        "user_question",
        "validation",
        "verification_completed",
    }
)

SIGNAL_PAYLOAD_KEYS = (
    "reason",
    "reason_summary",
    "goal",
    "decision",
    "progress_score",
    "passed",
    "question",
    "trigger",
    "root_cause",
    "corrective_instruction",
    "strategy",
    "tool",
    "error",
    "success",
    "commit",
    "message",
)

ADVISOR_ROLE = (
    "You are the recovery advisor of GCAE, a reversible coding runtime. One run is about to "
    "give up, and you can only see its trace: its state, its filtered event log, the "
    "validation and verification evidence, and what it already remembers failing at. The "
    "run's working context is deliberately not included, so judge the record, not the "
    "intent. Find what keeps failing, and prescribe the smallest correction that can still "
    "finish the task."
)

_EVENT_LINE = re.compile(r"^  \d\d:\d\d:\d\d ")


class RecoveryAction(StrEnum):
    """What the caller should do after a recovery attempt."""

    CONTINUE = "continue"  # a corrective step was queued; keep running
    ASK_USER = "ask_user"  # the run is waiting for the user
    FAILED = "failed"  # the run ended in failure
    UNAVAILABLE = "unavailable"  # no usable diagnosis; the caller chooses the next rung


class RecoveryAdvisor:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def diagnose(self, trace: str) -> Diagnosis:
        return self.provider.complete(build_recovery_prompt(trace), Diagnosis)


def build_recovery_prompt(trace: str) -> str:
    schema = json.dumps(Diagnosis.model_json_schema(), separators=(",", ":"))
    return (
        f"{ADVISOR_ROLE}\n"
        "Reply with exactly one JSON object and no other text.\n"
        "strategy: replan (queue corrective_instruction and continue the run), ask_user "
        "(only the user can decide), stop (the task cannot be done as stated).\n"
        "root_cause: what specifically keeps failing, citing the trace.\n"
        "corrective_instruction: one concrete action for the next attempt that differs from "
        "everything the trace shows was already tried. It becomes the next step's goal, so "
        "write it as an instruction, not as advice.\n"
        f"Diagnosis JSON schema: {schema}\n"
        f"Trace:\n{trace}"
    )


def build_trace(
    state: AgentState,
    events: Sequence[Event],
    memories: Sequence[MemoryRecord],
    candidate: str = "",
    max_events: int = 25,
    max_memories: int = 12,
    max_chars: int = 7000,
) -> str:
    """Render the run's own record, oldest first, bounded in size."""
    lines = [
        f"OBJECTIVE: {_clip(state.objective)}",
        f"REQUEST: {_clip(state.original_request)}",
        f"STATUS: {state.status} | PHASE: {state.phase.value} | ITERATION: {state.iteration}",
        f"ACCEPTED STEPS: {state.accepted_steps} | COMMIT: {state.accepted_commit or 'none'}",
    ]
    if state.hard_constraints:
        lines.append("CONSTRAINTS: " + _clip("; ".join(state.hard_constraints), 600))
    if state.success_criteria:
        lines.append("CRITERIA: " + _clip("; ".join(state.success_criteria), 600))
    if state.pending_question:
        lines.append(f"OPEN QUESTION: {_clip(state.pending_question)}")
    if state.plan:
        lines.append("PLAN:")
        lines.extend(
            f"  [{step.status}] {step.id}: {_clip(step.goal, 240)}"
            for step in state.plan[:12]
        )
    else:
        lines.append("PLAN: empty")

    validation = state.latest_validation
    if validation is not None:
        lines.append(
            "LAST VALIDATION: "
            f"passed={validation.passed} diff_check={validation.diff_check_passed} "
            f"changed={_clip(', '.join(validation.changed_files) or 'none', 300)} "
            f"dependencies={_clip(', '.join(validation.dependency_changes) or 'none', 200)} "
            f"scope_violations={_clip('; '.join(validation.scope_violations) or 'none', 300)}"
        )
        failed = [
            f"  {result.tool} exit={result.exit_code}: "
            f"{_clip((result.error or result.output or '').strip(), 400)}"
            for result in validation.command_results
            if not result.success
        ]
        if failed:
            lines.append("FAILED CHECKS:")
            lines.extend(failed[:6])
        if validation.details:
            lines.append("  " + _clip("; ".join(validation.details), 500))

    report = state.last_verification
    if report is not None:
        failed_criteria = "; ".join(
            item.criterion for item in report.criteria if not item.passed
        )
        lines.append(
            f"LAST VERIFICATION: passed={report.passed} hygiene={report.hygiene_passed} "
            f"missing={_clip('; '.join(report.missing_requirements) or 'none', 300)} "
            f"failed_criteria={_clip(failed_criteria or 'none', 300)}"
        )
        if report.details:
            lines.append("  " + _clip("; ".join(report.details), 500))
    if candidate:
        lines.append(f"CANDIDATE: {_clip(candidate, 300)}")

    recent = list(memories[-max_memories:]) if max_memories > 0 else []
    recent_ids = {record.id for record in recent}
    permanent = [
        record for record in memories if record.immutable and record.id not in recent_ids
    ]
    if permanent:
        lines.append("PERMANENT LESSONS:")
        lines.extend(f"  [{record.kind}] {_clip(record.content, 300)}" for record in permanent[-4:])
    if recent:
        lines.append("MEMORY (recent):")
        lines.extend(f"  [{record.kind}] {_clip(record.content, 300)}" for record in recent)

    tail = [
        f"  {_stamp(event.timestamp)} {event.event_type} {_payload(event)}"
        for event in events
        if event.event_type in SIGNAL_EVENTS
    ][-max_events:]
    if tail:
        lines.append(f"EVENTS (last {len(tail)}):")
        lines.extend(tail)
    return _fit(lines, max_chars)


def _payload(event: Event) -> str:
    data = {key: event.payload[key] for key in SIGNAL_PAYLOAD_KEYS if key in event.payload}
    return _clip(json.dumps(data, separators=(",", ":"), default=str), 300)


def _stamp(moment: datetime) -> str:
    return moment.strftime("%H:%M:%S")


def _clip(text: object, limit: int = 240) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _fit(lines: list[str], limit: int) -> str:
    """Drop the oldest event lines until the trace fits the character budget."""
    text = "\n".join(lines)
    while len(text) > limit:
        index = next((i for i, line in enumerate(lines) if _EVENT_LINE.match(line)), None)
        if index is None:
            return _clip(text, limit)
        del lines[index]
        text = "\n".join(lines)
    return text
