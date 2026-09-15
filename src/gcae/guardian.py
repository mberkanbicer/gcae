"""Runtime Guardian: deterministic health supervision for the agent loop.

The Guardian is NOT an agent. It solves no user task, plans nothing, writes no code and
makes no product decisions. It watches the *mechanism* — every model invocation, every
tool operation, every step boundary, every recovery — and answers one question: did the
machinery behave normally, and if not, which bounded recovery applies?

Design rules:

- Deterministic and dependency-light: it inspects values passed in by the runtime, never
  the controller. No LLM, no threads, no daemons of its own.
- It *decides* recoveries; the runtime *executes* them (repo, memory and provider access
  stay in ``Runtime``). This keeps supervision independent of the components it watches:
  a broken controller cannot break the check that catches it.
- Every recovery is bounded with an escalation path. The Guardian never retries forever
  and never recovers its own recovery indefinitely.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum


class HealthState(StrEnum):
    """Six states, no more. The TUI shows exactly one of these."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    RECOVERING = "recovering"
    WAITING = "waiting"
    BLOCKED = "blocked"
    FATAL = "fatal"


class ModelFailure(StrEnum):
    """What went wrong with a model invocation. Semantic judgement is NOT here."""

    NONE = "none"
    EMPTY_RESPONSE = "empty_response"
    OUTPUT_TRUNCATED = "output_truncated"
    SCHEMA_INVALID = "schema_invalid"
    STALL = "stall"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    CONTEXT_OVERFLOW = "context_overflow"
    NETWORK_FAILURE = "network_failure"
    PROVIDER_ERROR = "provider_error"
    UNKNOWN = "unknown"


class ActionFailure(StrEnum):
    """What went wrong with a tool/command operation, as mechanism — not meaning."""

    NONE = "none"
    COMMAND_FAILED = "command_failed"
    COMMAND_TIMEOUT = "command_timeout"
    PROCESS_HUNG = "process_hung"
    INTERACTIVE_INPUT_REQUIRED = "interactive_input_required"
    TOOL_EXCEPTION = "tool_exception"
    INVALID_PATH = "invalid_path"
    FILE_WRITE_FAILED = "file_write_failed"
    EXPECTED_ARTIFACT_MISSING = "expected_artifact_missing"
    GIT_STATE_CORRUPTED = "git_state_corrupted"
    WORKTREE_DIRTY_UNEXPECTEDLY = "worktree_dirty_unexpectedly"
    VALIDATION_FAILED = "validation_failed"
    NO_EXECUTION_EVIDENCE = "no_execution_evidence"
    ORPHAN_PROCESS = "orphan_process"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    """Bounded recoveries the Guardian may prescribe. Only mechanisms the runtime owns."""

    NONE = "none"
    RETRY_REQUEST = "retry_request"
    REPAIR_REQUEST = "repair_request"
    FALLBACK_PROVIDER = "fallback_provider"
    REBUILD_CONTEXT = "rebuild_context"
    REDUCE_CONTEXT = "reduce_context"
    RESTORE_CHECKPOINT = "restore_checkpoint"
    CLEAN_SPECULATIVE = "clean_speculative"
    SWITCH_SCRIPTED_INPUT = "switch_scripted_input"
    SWITCH_PTY = "switch_pty"
    TERMINATE_ORPHAN = "terminate_orphan"
    REOPEN_WORKTREE = "reopen_worktree"
    RELOAD_STATE = "reload_state"
    REOPEN_DATABASE = "reopen_database"
    REPLAN = "replan"
    WAIT_FOR_USER = "wait_for_user"
    MARK_BLOCKED = "mark_blocked"
    MARK_FATAL = "mark_fatal"


@dataclass(frozen=True)
class FailureCheckResult:
    """One deterministic verdict: what happened, how bad, what to do, which attempt."""

    ok: bool
    kind: str = "none"
    severity: str = "info"
    evidence: str = ""
    retryable: bool = False
    recovery_action: RecoveryAction = RecoveryAction.NONE
    attempt_count: int = 0


@dataclass
class GuardianConfig:
    """Bounds for every recovery type. Escalation is a table, not a hope."""

    model_retries: int = 2
    tool_retries: int = 1
    recovery_retries: int = 1
    stall_soft_seconds: float = 120.0
    stall_hard_seconds: float = 300.0
    context_reserve_tokens: int = 512


@dataclass
class Heartbeat:
    """Last-seen timestamps for everything the Guardian watches. In-memory only."""

    last_model_ok: float = 0.0
    last_tool_ok: float = 0.0
    last_persist_ok: float = 0.0
    last_checkpoint: float = 0.0
    last_verified_progress: float = 0.0
    last_activity: float = 0.0
    model_in_flight_since: float = 0.0
    process_active_since: float = 0.0


class Guardian:
    """In-process deterministic supervisor. See module docstring for the contract."""

    def __init__(self, config: GuardianConfig | None = None) -> None:
        self.config = config or GuardianConfig()
        self.health = HealthState.HEALTHY
        self.heartbeat = Heartbeat()
        self._attempts: dict[str, int] = {}
        self._last_recovery: str = ""
        self._last_recovery_ok: bool = True

    # ------------------------------------------------------------------ budgeting

    def allow(self, action: RecoveryAction, key: str, limit: int) -> tuple[bool, int]:
        """Bounded attempts per (action, key). Returns (allowed, attempt_count)."""
        bucket = f"{action.value}:{key}"
        count = self._attempts.get(bucket, 0) + 1
        self._attempts[bucket] = count
        return (count <= max(1, limit), count)

    def record_success(self, key_prefix: str = "") -> None:
        """A success clears budgets: a fixed problem must not poison later, different work."""
        if not key_prefix:
            self._attempts.clear()
            return
        for bucket in [name for name in self._attempts if name.startswith(key_prefix)]:
            del self._attempts[bucket]

    # ------------------------------------------------------------------ model path

    def pre_model_check(
        self,
        *,
        context_chars: int,
        context_budget_tokens: int,
        provider_name: str,
        schema_name: str,
        stale_lock: bool = False,
        cancelled: bool = False,
    ) -> FailureCheckResult:
        """Guardian.pre_model_check: is this request even safe to send?"""
        if cancelled:
            return FailureCheckResult(
                False, "cancelled", "info", "run was stopped", False, RecoveryAction.NONE
            )
        if stale_lock:
            return FailureCheckResult(
                False, "stale_lock", "error", "a stale run lock is present",
                False, RecoveryAction.MARK_BLOCKED,
            )
        if not provider_name:
            return FailureCheckResult(
                False, ModelFailure.PROVIDER_ERROR.value, "error",
                "no provider configured", False, RecoveryAction.MARK_BLOCKED,
            )
        if not schema_name:
            return FailureCheckResult(
                False, ModelFailure.SCHEMA_INVALID.value, "error",
                "no response schema for a structured call", False, RecoveryAction.MARK_BLOCKED,
            )
        estimated = max(1, (context_chars + 2) // 3)
        if estimated + self.config.context_reserve_tokens > context_budget_tokens:
            return FailureCheckResult(
                False, ModelFailure.CONTEXT_OVERFLOW.value, "warning",
                f"context ~{estimated} tokens exceeds budget {context_budget_tokens}",
                True, RecoveryAction.REDUCE_CONTEXT,
            )
        return FailureCheckResult(True)

    def post_model_check(
        self,
        *,
        output_text: str,
        elapsed_s: float,
        stall_timeout_s: float,
        error: str = "",
        truncated: bool = False,
        schema_error: str = "",
        status_code: int | None = None,
    ) -> FailureCheckResult:
        """Guardian.post_model_check: did the machinery deliver a usable response?"""
        lowered = f"{error}".lower()
        if status_code == 429 or "rate limit" in lowered or "429" in lowered:
            return FailureCheckResult(
                False, ModelFailure.RATE_LIMIT.value, "warning",
                error or "provider rate limited the request",
                True, RecoveryAction.RETRY_REQUEST,
            )
        if any(mark in lowered for mark in ("timeout", "timed out", "stalled", "stall")):
            kind = ModelFailure.STALL if "stall" in lowered else ModelFailure.TIMEOUT
            return FailureCheckResult(
                False, kind.value, "warning", error or "model request timed out",
                True, RecoveryAction.RETRY_REQUEST,
            )
        if any(
            mark in lowered
            for mark in ("connection", "network", "dns", "unreachable", "reset by peer")
        ):
            return FailureCheckResult(
                False, ModelFailure.NETWORK_FAILURE.value, "warning",
                error or "network failure during model request",
                True, RecoveryAction.RETRY_REQUEST,
            )
        if error:
            return FailureCheckResult(
                False, ModelFailure.PROVIDER_ERROR.value, "error", error,
                True, RecoveryAction.FALLBACK_PROVIDER,
            )
        if not output_text.strip():
            return FailureCheckResult(
                False, ModelFailure.EMPTY_RESPONSE.value, "warning",
                f"provider returned no text after {elapsed_s:.1f}s",
                True, RecoveryAction.RETRY_REQUEST,
            )
        if truncated:
            return FailureCheckResult(
                False, ModelFailure.OUTPUT_TRUNCATED.value, "warning",
                "response hit the output budget before completing",
                True, RecoveryAction.REPAIR_REQUEST,
            )
        if schema_error:
            return FailureCheckResult(
                False, ModelFailure.SCHEMA_INVALID.value, "warning", schema_error,
                True, RecoveryAction.REPAIR_REQUEST,
            )
        if elapsed_s >= stall_timeout_s > 0:
            return FailureCheckResult(
                False, ModelFailure.STALL.value, "warning",
                f"response took {elapsed_s:.1f}s without usable output",
                True, RecoveryAction.RETRY_REQUEST,
            )
        return FailureCheckResult(True)

    # ------------------------------------------------------------------ tool path

    def pre_tool_check(
        self,
        *,
        tool_name: str,
        worktree_exists: bool,
        path_inside_workspace: bool = True,
        command_allowed: bool = True,
        timeout_selected: bool = True,
    ) -> FailureCheckResult:
        """Guardian.pre_tool_check: is this operation safe to start?"""
        if not worktree_exists:
            return FailureCheckResult(
                False, ActionFailure.WORKTREE_DIRTY_UNEXPECTEDLY.value, "error",
                "worktree is missing before tool execution",
                True, RecoveryAction.REOPEN_WORKTREE,
            )
        if not path_inside_workspace:
            return FailureCheckResult(
                False, ActionFailure.INVALID_PATH.value, "error",
                f"{tool_name} targets a path outside the worktree",
                False, RecoveryAction.MARK_BLOCKED,
            )
        if not command_allowed:
            return FailureCheckResult(
                False, ActionFailure.INVALID_PATH.value, "error",
                "command is blocked by the guardrail",
                False, RecoveryAction.MARK_BLOCKED,
            )
        if not timeout_selected:
            return FailureCheckResult(
                False, ActionFailure.COMMAND_TIMEOUT.value, "warning",
                "no timeout mode selected for a command",
                True, RecoveryAction.RETRY_REQUEST,
            )
        return FailureCheckResult(True)

    def post_tool_check(
        self,
        *,
        tool_name: str,
        exit_code: int | None,
        timed_out: bool,
        waiting_for_input: bool,
        interactive_detected: bool,
        error: str = "",
        orphan_process: bool = False,
        worktree_sane: bool = True,
        evidence_produced: bool = True,
    ) -> FailureCheckResult:
        """Guardian.post_tool_check: did the operation behave — even on success?"""
        if orphan_process:
            return FailureCheckResult(
                False, ActionFailure.ORPHAN_PROCESS.value, "warning",
                f"{tool_name} left a live child behind",
                True, RecoveryAction.TERMINATE_ORPHAN,
            )
        if not worktree_sane:
            return FailureCheckResult(
                False, ActionFailure.GIT_STATE_CORRUPTED.value, "error",
                "worktree state is not what the operation left behind",
                True, RecoveryAction.RESTORE_CHECKPOINT,
            )
        if waiting_for_input or interactive_detected:
            return FailureCheckResult(
                True, ActionFailure.INTERACTIVE_INPUT_REQUIRED.value, "info",
                "program is asking for input; not a failure",
                False, RecoveryAction.SWITCH_SCRIPTED_INPUT,
            )
        if timed_out:
            return FailureCheckResult(
                False, ActionFailure.COMMAND_TIMEOUT.value, "warning",
                error or f"{tool_name} produced no progress before its timeout",
                True, RecoveryAction.TERMINATE_ORPHAN,
            )
        if exit_code not in (None, 0):
            return FailureCheckResult(
                False, ActionFailure.COMMAND_FAILED.value, "info",
                error or f"{tool_name} exited with {exit_code}",
                False, RecoveryAction.NONE,
            )
        if error and tool_name in {"write_file", "create_file", "apply_patch"}:
            return FailureCheckResult(
                False, ActionFailure.FILE_WRITE_FAILED.value, "error", error,
                False, RecoveryAction.MARK_BLOCKED,
            )
        if not evidence_produced:
            return FailureCheckResult(
                False, ActionFailure.NO_EXECUTION_EVIDENCE.value, "warning",
                f"{tool_name} produced nothing the next decision can use",
                False, RecoveryAction.NONE,
            )
        return FailureCheckResult(True)

    # ------------------------------------------------------------------ step boundary

    def step_check(
        self,
        *,
        state_serializable: bool,
        state_persisted: bool,
        commit_exists: bool,
        worktree_clean_or_expected_dirty: bool,
        memory_responsive: bool,
        evidence_linked: bool,
        trajectory_consistent: bool,
        orphan_processes: int = 0,
        stale_sessions: int = 0,
    ) -> FailureCheckResult:
        """Between semantic steps: prove the runtime is intact before continuing."""
        if not state_serializable:
            return self._fatal("run state is no longer serializable")
        if not state_persisted:
            return FailureCheckResult(
                False, "persistence_failure", "error",
                "state was not persisted at the step boundary",
                True, RecoveryAction.RELOAD_STATE,
            )
        if not commit_exists:
            return FailureCheckResult(
                False, ActionFailure.GIT_STATE_CORRUPTED.value, "error",
                "accepted commit is missing from the repository",
                True, RecoveryAction.RESTORE_CHECKPOINT,
            )
        if not worktree_clean_or_expected_dirty:
            return FailureCheckResult(
                False, ActionFailure.WORKTREE_DIRTY_UNEXPECTEDLY.value, "warning",
                "unexpected speculative files at the step boundary",
                True, RecoveryAction.CLEAN_SPECULATIVE,
            )
        if not memory_responsive:
            return FailureCheckResult(
                False, "persistence_failure", "error",
                "memory store is not responding",
                True, RecoveryAction.REOPEN_DATABASE,
            )
        if not evidence_linked:
            return FailureCheckResult(
                False, ActionFailure.NO_EXECUTION_EVIDENCE.value, "warning",
                "commands ran but no evidence was linked to the step",
                False, RecoveryAction.NONE,
            )
        if not trajectory_consistent:
            return FailureCheckResult(
                False, "trajectory_inconsistent", "warning",
                "trajectory record disagrees with plan state",
                True, RecoveryAction.REPLAN,
            )
        if orphan_processes:
            return FailureCheckResult(
                False, ActionFailure.ORPHAN_PROCESS.value, "warning",
                f"{orphan_processes} orphan processes at the step boundary",
                True, RecoveryAction.TERMINATE_ORPHAN,
            )
        if stale_sessions:
            return FailureCheckResult(
                False, "stale_session", "info",
                f"{stale_sessions} interactive sessions outlived their step",
                True, RecoveryAction.TERMINATE_ORPHAN,
            )
        return FailureCheckResult(True)

    # ------------------------------------------------------------------ recovery ops

    def verify_recovery(
        self,
        action: RecoveryAction,
        *,
        checks: dict[str, bool],
    ) -> FailureCheckResult:
        """Post-check a recovery action itself. Never assume a repair worked."""
        failed = sorted(name for name, passed in checks.items() if not passed)
        if not failed:
            return FailureCheckResult(True)
        if action in {RecoveryAction.RESTORE_CHECKPOINT, RecoveryAction.CLEAN_SPECULATIVE}:
            return FailureCheckResult(
                False, ActionFailure.GIT_STATE_CORRUPTED.value, "error",
                f"recovery {action.value} incomplete: {', '.join(failed)}",
                False, RecoveryAction.MARK_BLOCKED,
            )
        return FailureCheckResult(
            False, "recovery_incomplete", "warning",
            f"recovery {action.value} incomplete: {', '.join(failed)}",
            True, RecoveryAction.REPLAN,
        )

    # ------------------------------------------------------------------ heartbeat

    def note_activity(self, now: float | None = None) -> None:
        self.heartbeat.last_activity = now if now is not None else time.monotonic()

    def note_verified_progress(self, now: float | None = None) -> None:
        at = now if now is not None else time.monotonic()
        self.heartbeat.last_verified_progress = at
        self.heartbeat.last_activity = at

    def stall_status(self, now: float | None = None) -> str:
        """Combine signals: 'hard' (mechanism stuck), 'soft' (active, no progress), ''."""
        at = now if now is not None else time.monotonic()
        idle = at - (self.heartbeat.last_activity or at)
        since_progress = at - (self.heartbeat.last_verified_progress or at)
        if self.heartbeat.model_in_flight_since and (
            at - self.heartbeat.model_in_flight_since > self.config.stall_hard_seconds
        ):
            return "hard"
        if self.heartbeat.process_active_since and (
            at - self.heartbeat.process_active_since > self.config.stall_hard_seconds
        ):
            return "hard"
        if idle > self.config.stall_soft_seconds and since_progress > (
            self.config.stall_soft_seconds
        ):
            return "soft"
        return ""

    # ------------------------------------------------------------------ stale state

    def stale_repairs(
        self,
        *,
        ui_shows_model_active: bool,
        model_actually_active: bool,
        ui_shows_process_active: bool,
        process_actually_active: bool,
    ) -> list[str]:
        """Names of stale UI/runtime states to repair. Never leave 'generating…' stuck."""
        repairs: list[str] = []
        if ui_shows_model_active and not model_actually_active:
            repairs.append("clear_model_active")
        if ui_shows_process_active and not process_actually_active:
            repairs.append("clear_process_active")
        return repairs

    # ------------------------------------------------------------------ health

    def set_health(self, state: HealthState) -> HealthState:
        self.health = state
        return state

    def health_for(
        self, *, blocked: bool, waiting: bool, recovering: bool, degraded: bool
    ) -> HealthState:
        """Single priority for the top bar: fatal/blocked/waiting/recovering first."""
        if self.health is HealthState.FATAL:
            return HealthState.FATAL
        if blocked:
            return HealthState.BLOCKED
        if waiting:
            return HealthState.WAITING
        if recovering:
            return HealthState.RECOVERING
        if degraded:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    @staticmethod
    def _fatal(evidence: str) -> FailureCheckResult:
        return FailureCheckResult(
            False, "fatal", "error", evidence, False, RecoveryAction.MARK_FATAL
        )

    # ------------------------------------------------------------------ plan health

    def plan_health(
        self,
        *,
        steps: list[dict[str, object]],
        history: list[dict[str, object]],
        version: int,
        current_step_id: str | None,
        accepted_commit: str | None,
        criteria: list[str],
        verified_criteria: list[str],
    ) -> FailureCheckResult:
        """Review plan consistency after acceptance, rollback, replan, resume, override.

        Fails closed (block) on corruption: a plan that disagrees with itself or its
        checkpoint must not execute another step until a human looks at it.
        """
        del accepted_commit  # checkpoint mapping is checked per-step below
        ids = [str(step.get("id")) for step in steps]
        if len(set(ids)) != len(ids):
            return FailureCheckResult(
                False, "plan_history_corrupted", "error",
                "duplicate step ids in the active plan",
                False, RecoveryAction.MARK_BLOCKED,
            )
        try:
            last_version = history[-1].get("version", 0) if history else 0
            version_seen = int(str(last_version))
        except (TypeError, ValueError):
            version_seen = -1
        if history and version_seen != version:
            return FailureCheckResult(
                False, "plan_history_corrupted", "error",
                f"plan v{version} disagrees with history v{history[-1].get('version')}",
                False, RecoveryAction.MARK_BLOCKED,
            )
        by_id = {str(step.get("id")): step for step in steps}
        for step in steps:
            depends = step.get("depends_on") or []
            for dep in depends if isinstance(depends, list) else []:
                if str(dep) not in by_id:
                    return FailureCheckResult(
                        False, "invalid_dependency", "error",
                        f"step {step.get('id')} depends on unknown {dep}",
                        False, RecoveryAction.MARK_BLOCKED,
                    )
        if current_step_id is not None:
            current = by_id.get(current_step_id)
            if current is None or str(current.get("status")) not in {"pending", "active"}:
                return FailureCheckResult(
                    False, "current_step_missing", "error",
                    f"current step {current_step_id} is not an executable plan step",
                    False, RecoveryAction.MARK_BLOCKED,
                )
        for step in steps:
            if str(step.get("status")) == "completed" and not step.get("checkpoint"):
                return FailureCheckResult(
                    False, "plan_checkpoint_mismatch", "error",
                    f"completed step {step.get('id')} has no checkpoint linkage",
                    False, RecoveryAction.MARK_BLOCKED,
                )
        unresolved = [c for c in criteria if c not in verified_criteria]
        if unresolved and any(
            str(step.get("status")) in {"pending", "active"} for step in steps
        ):
            covered: set[str] = set()
            for step in steps:
                if str(step.get("status")) in {"pending", "active"}:
                    requirements = step.get("validation_requirements") or []
                    if isinstance(requirements, list):
                        covered.update(str(item) for item in requirements)
            missing = [c for c in unresolved if c not in covered]
            if missing:
                return FailureCheckResult(
                    False, "missing_success_criterion", "error",
                    f"no open step covers: {', '.join(missing[:3])}",
                    False, RecoveryAction.MARK_BLOCKED,
                )
        return FailureCheckResult(True)

    # ------------------------------------------------------------------ introspection

    def attempt_counts(self) -> dict[str, int]:
        return dict(self._attempts)
