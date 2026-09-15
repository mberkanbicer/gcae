"""Stable plan history and partial replanning (§60–67).

The plan is structured trajectory state, not disposable text: verified prefixes survive
replans with stable IDs, invalidation needs evidence, and rollback boundaries agree with
plan boundaries.
"""

import subprocess
from pathlib import Path

from gcae.models import PlanStep, ReplanPatch, StepInvalidation
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl


def init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def start_runtime(tmp_path: Path, criteria: list[str] | None = None) -> Runtime:
    source = tmp_path / "source"
    base = init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    runtime.start("do the work", success_criteria=criteria or ["file exists: out.txt"])
    assert runtime.state is not None
    runtime.state.accepted_commit = base
    return runtime


def completed(step_id: str, goal: str, commit: str, **extra: object) -> PlanStep:
    return PlanStep(
        id=step_id, goal=goal, status="completed", checkpoint=commit, locked=True,
        **extra,  # type: ignore[arg-type]
    )


def test_prefix_preserved_across_replan(tmp_path: Path) -> None:
    """§60: S1✓ S2✓ survive an S3 failure with identical IDs and statuses."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        completed("step-2", "reproduce", base),
        PlanStep(id="step-3", goal="fix", status="active"),
        PlanStep(id="step-4", goal="verify", status="pending"),
    ]
    runtime.state.next_step_number = 50
    runtime.state.next_step_number = 5
    before = [(s.id, s.status, s.goal) for s in runtime.state.plan]
    assert runtime._replan(
        runtime.state.plan[2], "S3 approach failed", failed=True
    ) is False
    after = [(s.id, s.status, s.goal) for s in runtime.state.plan]
    assert after[0] == before[0] and after[1] == before[1]
    assert ("step-3", "replaced", "fix") in after
    assert runtime.state.plan[2].replaced_by == "step-5"
    assert any(sid == "step-4" and status == "pending" for sid, status, _ in after)
    assert any(sid == "step-5" and status == "pending" for sid, status, _ in after)
    assert runtime.state.plan_version == 2
    record = runtime.state.plan_history[-1]
    assert record.preserved == ["step-1", "step-2"]
    assert record.replaced == ["step-3"]
    assert record.inserted == ["step-5"]


def test_earlier_invalidation_takes_dependents_with_it(tmp_path: Path) -> None:
    """§61: evidence against S2 invalidates S2 and dependent S3; S1 stays valid."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        completed("step-2", "data model", base),
        completed("step-3", "cli", base, depends_on=["step-2"]),
        PlanStep(id="step-4", goal="validate", status="active"),
    ]
    runtime.state.next_step_number = 50
    assert runtime._invalidate_step("step-2", reason="model cannot stream", evidence_ids=[7])
    assert runtime._replan(
        runtime.state.plan[3], "model cannot stream", failed=True,
        invalidate=[
            StepInvalidation(step_id="step-2", reason="model cannot stream", evidence_ids=[7])
        ],
    ) is False
    by_id = {s.id: s for s in runtime.state.plan}
    assert by_id["step-1"].status == "completed"
    assert by_id["step-2"].status == "invalidated"
    assert by_id["step-2"].invalidated_reason == "model cannot stream"
    assert by_id["step-2"].invalidated_evidence_ids == [7]
    assert by_id["step-3"].status == "invalidated", "dependents follow"
    assert "step-2" in by_id["step-3"].invalidated_reason
    assert runtime.state.plan_version == 2
    assert runtime.state.plan_history[-1].invalidated == ["step-2", "step-3"]


def test_unrelated_past_steps_survive_a_local_failure(tmp_path: Path) -> None:
    """§62: an S4 failure changes nothing about S1–S3, and no checkpoint moves."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        completed("step-2", "model", base),
        completed("step-3", "ui", base),
        PlanStep(id="step-4", goal="scoring", status="active"),
    ]
    runtime.state.next_step_number = 50
    runtime._replan(runtime.state.plan[3], "scoring bug", failed=True)
    by_id = {s.id: s for s in runtime.state.plan}
    for sid in ("step-1", "step-2", "step-3"):
        assert by_id[sid].status == "completed"
        assert by_id[sid].checkpoint == base
    assert runtime.state.accepted_commit == base, "no rollback without need"


def test_replan_without_rollback_keeps_trusted_state(tmp_path: Path) -> None:
    """§63: future-only changes leave the trusted checkpoint and history alone."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        PlanStep(id="step-2", goal="build", status="active"),
    ]
    runtime.state.next_step_number = 50
    runtime._replan(runtime.state.plan[1], "try another route", failed=False)
    assert runtime.state.accepted_commit == base
    assert runtime.state.plan[0].status == "completed"
    assert any(s.status == "pending" for s in runtime.state.plan)


def test_rollback_without_replan_keeps_the_step_active(tmp_path: Path) -> None:
    """§64: a bad candidate rolls back; the semantic goal stays active, no new version."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None and runtime.repo is not None
    runtime.state.plan = [PlanStep(id="step-1", goal="build", status="active")]
    version = runtime.state.plan_version
    (Path(runtime.state.worktree) / "scratch.txt").write_text("speculative")
    runtime._rollback("candidate bad")
    assert runtime.state.plan_version == version, "rollback is not a replan"
    assert runtime.state.plan[0].status == "active"
    assert not (Path(runtime.state.worktree) / "scratch.txt").exists()


def test_user_override_preserves_completed_work(tmp_path: Path) -> None:
    """§65: an override replaces pending/active/future, never verified history."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        PlanStep(id="step-2", goal="build", status="active"),
        PlanStep(id="step-3", goal="verify", status="pending"),
    ]
    runtime.state.next_step_number = 50
    runtime.inject_user_instruction("use textual instead")
    by_id = {s.id: s for s in runtime.state.plan}
    assert by_id["step-1"].status == "completed"
    assert by_id["step-2"].status == "skipped"
    assert by_id["step-3"].status == "skipped"
    assert runtime.state.plan_version == 2
    assert runtime.state.plan_history[-1].reason_category == "user_override"


def test_stale_patch_is_rejected_without_touching_the_plan(tmp_path: Path) -> None:
    """§57: a patch against an old version cannot mutate the active plan."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        PlanStep(id="step-2", goal="build", status="active"),
    ]
    before = runtime.state.plan_version
    patch = ReplanPatch(
        base_plan_version=before - 1, reason="stale", affected_from_step_id="step-2",
        preserve_step_ids=["step-1"], replace_step_ids=["step-2"],
        new_steps=[PlanStep(id="step-9", goal="other", status="pending")],
    )
    assert runtime._apply_replan_patch(patch, source="test") is False
    assert runtime.state.plan_version == before
    assert [s.id for s in runtime.state.plan] == ["step-1", "step-2"]
    assert runtime.state.plan[1].status == "active"


def test_locked_rewrite_without_evidence_is_rejected(tmp_path: Path) -> None:
    """§53: the model cannot delete verified history by omitting it."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        PlanStep(id="step-2", goal="build", status="active"),
    ]
    patch = ReplanPatch(
        base_plan_version=runtime.state.plan_version, reason="cleaner",
        affected_from_step_id="step-1", preserve_step_ids=[],
        replace_step_ids=["step-1"],
        new_steps=[PlanStep(id="step-9", goal="other", status="pending")],
    )
    assert runtime._apply_replan_patch(patch, source="test") is False
    assert runtime.state.plan[0].status == "completed"


def test_resume_restores_plan_versions_and_mapping(tmp_path: Path) -> None:
    """§66: resume restores ids, statuses, versions, history and checkpoint mapping."""
    runtime = start_runtime(tmp_path)
    assert runtime.state is not None
    base = runtime.state.accepted_commit or ""
    runtime.state.plan = [
        completed("step-1", "inspect", base),
        PlanStep(id="step-2", goal="build", status="pending"),
    ]
    runtime.state.plan_version = 3
    from gcae.models import PlanVersion

    runtime.state.plan_history = [
        PlanVersion(version=2, reason="r", preserved=["step-1"], replaced=[], inserted=["step-2"]),
        PlanVersion(version=3, reason="r2", preserved=["step-1"], replaced=[], inserted=[]),
    ]
    runtime.state.current_step_id = "step-2"
    runtime._persist()
    run_id = runtime.state.run_id
    resumed = Runtime(
        tmp_path / "source", tmp_path / "runtime", provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    resumed.resume(run_id)
    assert resumed.state is not None
    assert [(s.id, s.status) for s in resumed.state.plan] == [
        ("step-1", "completed"), ("step-2", "pending"),
    ]
    assert resumed.state.plan_version == 3
    assert len(resumed.state.plan_history) == 2
    assert resumed.state.accepted_commit == base
    assert resumed.state.current_step_id == "step-2"
