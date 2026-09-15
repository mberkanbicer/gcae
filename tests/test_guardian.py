"""Runtime Guardian: deterministic supervision, bounded recovery, real health."""

import time
from pathlib import Path

from gcae.guardian import (
    ActionFailure,
    FailureCheckResult,
    Guardian,
    GuardianConfig,
    HealthState,
    ModelFailure,
    RecoveryAction,
)


def test_empty_model_output_is_caught_before_the_controller() -> None:
    guardian = Guardian()
    check = guardian.post_model_check(
        output_text="   ", elapsed_s=3.2, stall_timeout_s=45.0
    )
    assert not check.ok
    assert check.kind == ModelFailure.EMPTY_RESPONSE.value
    assert check.retryable
    assert check.recovery_action is RecoveryAction.RETRY_REQUEST


def test_truncated_output_requests_repair_not_blind_retry() -> None:
    guardian = Guardian()
    check = guardian.post_model_check(
        output_text='{"action": "exec',
        elapsed_s=12.0,
        stall_timeout_s=45.0,
        truncated=True,
    )
    assert check.kind == ModelFailure.OUTPUT_TRUNCATED.value
    assert check.recovery_action is RecoveryAction.REPAIR_REQUEST


def test_rate_limit_schema_and_context_failures_classify_distinctly() -> None:
    guardian = Guardian()
    limited = guardian.post_model_check(
        output_text="", elapsed_s=1.0, stall_timeout_s=45.0,
        error="429 rate limit exceeded",
    )
    assert limited.kind == ModelFailure.RATE_LIMIT.value
    broken = guardian.post_model_check(
        output_text='{"nope": true}', elapsed_s=1.0, stall_timeout_s=45.0,
        schema_error="missing required field 'action'",
    )
    assert broken.kind == ModelFailure.SCHEMA_INVALID.value
    assert broken.recovery_action is RecoveryAction.REPAIR_REQUEST
    pre = guardian.pre_model_check(
        context_chars=60000, context_budget_tokens=8192,
        provider_name="m", schema_name="Decision",
    )
    assert not pre.ok and pre.kind == ModelFailure.CONTEXT_OVERFLOW.value
    assert pre.recovery_action is RecoveryAction.REDUCE_CONTEXT


def test_command_hang_means_terminate_orphan_not_retry() -> None:
    guardian = Guardian()
    check = guardian.post_tool_check(
        tool_name="run_command", exit_code=None, timed_out=True,
        waiting_for_input=False, interactive_detected=False,
        error="idle timeout after 20.0s",
    )
    assert check.kind == ActionFailure.COMMAND_TIMEOUT.value
    assert check.recovery_action is RecoveryAction.TERMINATE_ORPHAN


def test_interactive_wait_is_healthy_not_a_failure() -> None:
    guardian = Guardian()
    check = guardian.post_tool_check(
        tool_name="run_command", exit_code=None, timed_out=False,
        waiting_for_input=True, interactive_detected=True,
    )
    assert check.ok
    assert check.kind == ActionFailure.INTERACTIVE_INPUT_REQUIRED.value


def test_exited_command_still_gets_a_health_check() -> None:
    guardian = Guardian()
    leaked = guardian.post_tool_check(
        tool_name="run_command", exit_code=0, timed_out=False,
        waiting_for_input=False, interactive_detected=False, orphan_process=True,
    )
    assert not leaked.ok
    assert leaked.kind == ActionFailure.ORPHAN_PROCESS.value
    corrupted = guardian.post_tool_check(
        tool_name="write_file", exit_code=0, timed_out=False,
        waiting_for_input=False, interactive_detected=False, worktree_sane=False,
    )
    assert corrupted.kind == ActionFailure.GIT_STATE_CORRUPTED.value
    assert corrupted.recovery_action is RecoveryAction.RESTORE_CHECKPOINT


def test_step_boundary_violation_preserves_the_checkpoint() -> None:
    guardian = Guardian()
    check = guardian.step_check(
        state_serializable=True, state_persisted=True, commit_exists=False,
        worktree_clean_or_expected_dirty=True, memory_responsive=True,
        evidence_linked=True, trajectory_consistent=True,
    )
    assert not check.ok
    assert check.recovery_action is RecoveryAction.RESTORE_CHECKPOINT
    silent = guardian.step_check(
        state_serializable=True, state_persisted=False, commit_exists=True,
        worktree_clean_or_expected_dirty=True, memory_responsive=True,
        evidence_linked=True, trajectory_consistent=True,
    )
    assert silent.recovery_action is RecoveryAction.RELOAD_STATE


def test_memory_failure_is_detected_and_bounded() -> None:
    guardian = Guardian()
    check = guardian.step_check(
        state_serializable=True, state_persisted=True, commit_exists=True,
        worktree_clean_or_expected_dirty=True, memory_responsive=False,
        evidence_linked=True, trajectory_consistent=True,
    )
    assert not check.ok
    assert check.recovery_action is RecoveryAction.REOPEN_DATABASE


def test_recovery_itself_is_verified_not_assumed() -> None:
    guardian = Guardian()
    assert guardian.verify_recovery(
        RecoveryAction.RESTORE_CHECKPOINT,
        checks={"clean": True, "head_matches": True},
    ).ok
    failed = guardian.verify_recovery(
        RecoveryAction.RESTORE_CHECKPOINT,
        checks={"clean": True, "head_matches": False},
    )
    assert not failed.ok
    assert failed.recovery_action is RecoveryAction.MARK_BLOCKED


def test_recovery_budgets_escalate_then_stop() -> None:
    guardian = Guardian(config=GuardianConfig(model_retries=2))
    allowed_first, first = guardian.allow(RecoveryAction.RETRY_REQUEST, "ctrl", 2)
    allowed_second, second = guardian.allow(RecoveryAction.RETRY_REQUEST, "ctrl", 2)
    allowed_third, third = guardian.allow(RecoveryAction.RETRY_REQUEST, "ctrl", 2)
    assert (allowed_first, first) == (True, 1)
    assert (allowed_second, second) == (True, 2)
    assert (allowed_third, third) == (False, 3)
    guardian.record_success("retry_request")
    allowed_again, _ = guardian.allow(RecoveryAction.RETRY_REQUEST, "ctrl", 2)
    assert allowed_again, "a fixed problem must not poison later work"


def test_soft_and_hard_stalls_use_combined_signals() -> None:
    guardian = Guardian(config=GuardianConfig(stall_soft_seconds=10.0, stall_hard_seconds=30.0))
    now = time.monotonic()
    guardian.heartbeat.last_activity = now - 60.0
    guardian.heartbeat.last_verified_progress = now - 60.0
    assert guardian.stall_status(now) == "soft"
    guardian.heartbeat.model_in_flight_since = now - 60.0
    assert guardian.stall_status(now) == "hard"
    fresh = Guardian()
    fresh.note_verified_progress(now)
    assert fresh.stall_status(now) == ""


def test_stale_ui_state_is_named_for_repair() -> None:
    guardian = Guardian()
    repairs = guardian.stale_repairs(
        ui_shows_model_active=True, model_actually_active=False,
        ui_shows_process_active=True, process_actually_active=False,
    )
    assert repairs == ["clear_model_active", "clear_process_active"]
    assert guardian.stale_repairs(
        ui_shows_model_active=True, model_actually_active=True,
        ui_shows_process_active=False, process_actually_active=False,
    ) == []


def _health(guardian: Guardian, **flags: bool) -> HealthState:
    base = {"blocked": False, "waiting": False, "recovering": False, "degraded": False}
    base.update(flags)
    return guardian.health_for(**base)  # type: ignore[arg-type]


def test_health_priority_is_total() -> None:
    guardian = Guardian()
    assert _health(guardian) is HealthState.HEALTHY
    assert _health(guardian, degraded=True) is HealthState.DEGRADED
    assert _health(guardian, recovering=True, degraded=True) is HealthState.RECOVERING
    assert _health(guardian, waiting=True, recovering=True) is HealthState.WAITING
    assert _health(guardian, blocked=True, waiting=True) is HealthState.BLOCKED
    guardian.set_health(HealthState.FATAL)
    assert _health(guardian) is HealthState.FATAL


def test_guardian_check_result_shape() -> None:
    result = FailureCheckResult(
        ok=False, kind="x", recovery_action=RecoveryAction.REPLAN, attempt_count=2
    )
    assert result.attempt_count == 2
    assert result.severity == "info"


def test_step_check_catches_dirty_orphans_and_drift() -> None:
    guardian = Guardian()
    dirty = guardian.step_check(
        state_serializable=True, state_persisted=True, commit_exists=True,
        worktree_clean_or_expected_dirty=False, memory_responsive=True,
        evidence_linked=True, trajectory_consistent=True,
    )
    assert dirty.recovery_action is RecoveryAction.CLEAN_SPECULATIVE
    leaked = guardian.step_check(
        state_serializable=True, state_persisted=True, commit_exists=True,
        worktree_clean_or_expected_dirty=True, memory_responsive=True,
        evidence_linked=True, trajectory_consistent=True, orphan_processes=1,
    )
    assert leaked.kind == ActionFailure.ORPHAN_PROCESS.value
    drifted = guardian.step_check(
        state_serializable=True, state_persisted=True, commit_exists=True,
        worktree_clean_or_expected_dirty=True, memory_responsive=True,
        evidence_linked=True, trajectory_consistent=False,
    )
    assert drifted.recovery_action is RecoveryAction.REPLAN
    assert drifted.retryable


def test_command_hang_terminates_and_reaps_the_process() -> None:
    import time

    from gcae.execution import CommandRequest, CommandRunner, ExecutionMode

    runner = CommandRunner(
        "/tmp", default_timeout=30.0, idle_timeout=0.3, startup_timeout=5.0
    )
    request = CommandRequest(
        command="sleep 30", mode=ExecutionMode.BATCH,
        timeout=30.0, idle_timeout=0.3, startup_timeout=5.0,
    )
    running = runner.start(request)
    outcome = running.wait(
        timeout=30.0, idle_timeout=0.3, startup_timeout=5.0,
        allow_prompt_wait=False,
    )
    assert outcome.timed_out
    assert outcome.termination_reason, "termination must be recorded, not silent"
    deadline = time.monotonic() + 5
    while running.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert running.poll() is not None, "no orphan may survive a timeout"
    guardian = Guardian()
    check = guardian.post_tool_check(
        tool_name="run_command", exit_code=outcome.exit_code,
        timed_out=outcome.timed_out, waiting_for_input=False,
        interactive_detected=False, error="idle timeout",
    )
    assert check.kind == ActionFailure.COMMAND_TIMEOUT.value


def test_empty_model_output_recovers_through_the_ladder(tmp_path) -> None:
    """Integration: an unusable controller response triggers a guardian check and the
    run recovers via the advisor instead of feeding the evaluator garbage."""
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    provider = FakeProvider(
        [
            {"nonsense": True},
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {"name": "create_file",
                         "arguments": {"path": "out.txt", "content": "ok\n"}},
            },
            {"action": "complete_semantic_step", "semantic_goal": "create",
             "reason_summary": "done"},
            {"action": "finish_candidate", "semantic_goal": "create",
             "reason_summary": "finish"},
        ],
        repair_limit=0,
    )
    advisor = FakeProvider(
        [
            {
                "root_cause": "controller produced unusable output",
                "corrective_instruction": "produce a valid tool decision",
                "strategy": "replan",
            }
        ]
    )
    runtime = Runtime(
        source, tmp_path / "runtime", provider=provider,
        role_providers={"recovery": advisor}, control=RuntimeControl(),
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    state = runtime.run()
    assert state.status == "complete", state.status
    kinds = [event.event_type for event in events]
    assert "guardian_check" in kinds, "the failed mechanism check must be visible"
    assert "recovery_started" in kinds
    assert "health_changed" in kinds
    check = next(e for e in events if e.event_type == "guardian_check")
    assert check.payload["subject"] == "model:controller"
    assert (Path(state.worktree) / "out.txt").read_text() == "ok\n"


def _plan_step(
    sid: str, status: str, checkpoint: str | None = None, depends: list[str] | None = None,
    requirements: list[str] | None = None,
) -> dict[str, object]:
    step: dict[str, object] = {"id": sid, "status": status, "goal": sid}
    if checkpoint:
        step["checkpoint"] = checkpoint
    if depends:
        step["depends_on"] = depends
    if requirements:
        step["validation_requirements"] = requirements
    return step


def test_plan_health_reviews_every_invariant_directly() -> None:
    guardian = Guardian()
    healthy = guardian.plan_health(
        steps=[
            _plan_step("s1", "completed", checkpoint="a1"),
            _plan_step("s2", "active", requirements=["file exists: out.txt"]),
        ],
        history=[{"version": 2}],
        version=2,
        current_step_id="s2",
        accepted_commit="a1",
        criteria=["file exists: out.txt"],
        verified_criteria=[],
    )
    assert healthy.ok
    duplicated = guardian.plan_health(
        steps=[_plan_step("s1", "active"), _plan_step("s1", "pending")],
        history=[], version=1, current_step_id="s1",
        accepted_commit=None, criteria=[], verified_criteria=[],
    )
    assert duplicated.kind == "plan_history_corrupted"
    assert duplicated.recovery_action is RecoveryAction.MARK_BLOCKED
    mismatched = guardian.plan_health(
        steps=[_plan_step("s1", "active")],
        history=[{"version": 3}],
        version=2, current_step_id="s1",
        accepted_commit=None, criteria=[], verified_criteria=[],
    )
    assert mismatched.kind == "plan_history_corrupted"
    dangling = guardian.plan_health(
        steps=[_plan_step("s1", "active", depends=["s0"])],
        history=[], version=1, current_step_id="s1",
        accepted_commit=None, criteria=[], verified_criteria=[],
    )
    assert dangling.kind == "invalid_dependency"
    stale_current = guardian.plan_health(
        steps=[
            _plan_step("s1", "completed", checkpoint="a1"),
            _plan_step("s2", "completed", checkpoint="a2"),
        ],
        history=[], version=1, current_step_id="s2",
        accepted_commit="a2", criteria=[], verified_criteria=[],
    )
    assert stale_current.kind == "current_step_missing"
    unlinked = guardian.plan_health(
        steps=[_plan_step("s1", "completed")],
        history=[], version=1, current_step_id=None,
        accepted_commit=None, criteria=[], verified_criteria=[],
    )
    assert unlinked.kind == "plan_checkpoint_mismatch"
    uncovered = guardian.plan_health(
        steps=[_plan_step("s1", "active", requirements=["unrelated"])],
        history=[], version=1, current_step_id="s1",
        accepted_commit=None,
        criteria=["file exists: out.txt"], verified_criteria=[],
    )
    assert uncovered.kind == "missing_success_criterion"


def test_event_store_failure_is_detected_and_visible(tmp_path) -> None:
    """§78: events.jsonl write failure must surface, not silently continue as healthy."""
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {"name": "create_file",
                         "arguments": {"path": "out.txt", "content": "ok\n"}},
            },
            {"action": "complete_semantic_step", "semantic_goal": "create",
             "reason_summary": "done"},
            {"action": "finish_candidate", "semantic_goal": "create",
             "reason_summary": "finish"},
        ]
    )
    runtime = Runtime(
        source, tmp_path / "runtime", provider=provider, control=RuntimeControl(),
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    log_path = tmp_path / "runtime" / "runs" / runtime.state.run_id / "events.jsonl"
    log_path.unlink()
    log_path.mkdir()  # append mode can no longer open the path
    state = runtime.run()
    assert state.status == "complete", state.status
    degraded = [e for e in events if e.event_type == "runtime_degraded"]
    assert degraded, "the lost event store must be reported, not ignored"
    assert degraded[0].payload["component"] == "event log"
    assert any("event log failed" in d for d in state.degradations), (
        "the run must not look healthy while its own record is unwritable"
    )
    log_path.rmdir()
