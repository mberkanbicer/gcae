"""Resume robustness (v0.6.0): a run killed at any point resumes deterministically.

Each test constructs the exact on-disk aftermath of a crash at a specific window
(commit-without-persist, torn event line, stale write temp) and asserts that
resume reconciles it and *explains* the reconciliation.
"""

import json
import subprocess
from pathlib import Path

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


def start_runtime(tmp_path: Path) -> tuple[Runtime, str, str]:
    """A started run with a trusted checkpoint; returns (runtime, run_id, base commit)."""
    source = tmp_path / "source"
    base = init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider([]), control=RuntimeControl(),
    )
    runtime.start("do the work", success_criteria=["file exists: out.txt"])
    assert runtime.state is not None
    runtime.state.accepted_commit = base
    runtime._persist()
    return runtime, runtime.state.run_id, base


def resumed_runtime(tmp_path: Path, run_id: str) -> tuple[Runtime, list]:
    """A second Runtime that resumes the run, collecting its events."""
    runtime = Runtime(
        tmp_path / "source", tmp_path / "runtime", provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.resume(run_id)
    return runtime, events


def test_unrecorded_checkpoint_is_discarded_and_reported(tmp_path: Path) -> None:
    """W1: crash after the checkpoint commit, before state.json was written."""
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    source = tmp_path / "source"
    # a checkpoint commit landed on the branch but state.json still trusts `base`
    tree = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{base}^{{tree}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    phantom = subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit-tree", tree, "-p", base, "-m", "gcae: later step"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(source), "update-ref", f"refs/heads/gcae/{run_id}", phantom],
        check=True,
    )
    resumed, events = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    reconciled = [e for e in events if e.event_type == "resume_reconciled"]
    assert len(reconciled) == 1
    assert reconciled[0].payload["action"] == "discarded_unrecorded_checkpoint"
    assert reconciled[0].payload["subject"] == "gcae: later step"
    assert resumed.repo is not None
    assert resumed.repo.current_commit() == base, "the unrecorded commit is gone"
    assert not resumed.repo.status(), "worktree is clean at the trusted checkpoint"


def test_diverged_branch_is_restored_and_reported(tmp_path: Path) -> None:
    """A branch moved behind its trusted checkpoint is restored forward, and says so."""
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    source = tmp_path / "source"
    # two commits beyond base: state trusts the newer one, branch points at base
    tree = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{base}^{{tree}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    trusted = subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit-tree", tree, "-p", base, "-m", "gcae: accepted"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    runtime.state.accepted_commit = trusted
    runtime._persist()
    # branch stays at base while state trusts `trusted`: resume must restore forward
    resumed, events = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    reconciled = [e for e in events if e.event_type == "resume_reconciled"]
    assert reconciled[0].payload["action"] == "branch_diverged_from_trusted_checkpoint"
    assert resumed.repo is not None
    assert resumed.repo.current_commit() == trusted


def test_torn_event_line_is_repaired_not_inherited(tmp_path: Path) -> None:
    """W6: a crash mid-append leaves a torn last line; resume truncates it."""
    runtime, run_id, _ = start_runtime(tmp_path)
    assert runtime.state is not None
    log = tmp_path / "runtime" / "runs" / run_id / "events.jsonl"
    with log.open("ab") as handle:
        handle.write(b'{"run_id": "tor')  # no newline, not JSON
    resumed, events = resumed_runtime(tmp_path, run_id)
    lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line]
    assert all(json.loads(line) for line in lines), "no debris survives the repair"
    repaired = [
        e for e in events
        if e.event_type == "resume_reconciled"
        and e.payload["action"] == "truncated_torn_event_line"
    ]
    assert repaired and repaired[0].payload["bytes"] > 0
    assert resumed.state is not None


def test_stale_state_write_temp_is_removed(tmp_path: Path) -> None:
    """A crash between the atomic state write's two steps leaves debris; resume cleans it."""
    runtime, run_id, _ = start_runtime(tmp_path)
    assert runtime.state is not None
    stale = tmp_path / "runtime" / "runs" / run_id / "state.json.write"
    stale.write_text("{ half-written", encoding="utf-8")
    resumed, _ = resumed_runtime(tmp_path, run_id)
    assert not stale.exists()
    assert resumed.state is not None


def test_plan_ahead_of_git_is_invalidated_on_resume(tmp_path: Path) -> None:
    """W5: the plan claims a completed step at a checkpoint the trusted commit lacks."""
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    source = tmp_path / "source"
    tree = subprocess.run(
        ["git", "-C", str(source), "rev-parse", f"{base}^{{tree}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    c2 = subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit-tree", tree, "-p", base, "-m", "gcae: step 2"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(source), "update-ref", f"refs/heads/gcae/{run_id}", c2],
        check=True,
    )
    from gcae.models import PlanStep

    runtime.state.plan = [
        PlanStep(id="step-1", goal="inspect", status="completed",
                 checkpoint=base, locked=True),
        PlanStep(id="step-2", goal="build", status="completed",
                 checkpoint=c2, locked=True),
        PlanStep(id="step-3", goal="verify", status="pending"),
    ]
    runtime.state.current_step_id = "step-3"
    runtime.state.accepted_commit = base  # state trusts only base: step-2 is not proven
    runtime._persist()
    resumed, events = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    steps = {s.id: s for s in resumed.state.plan}
    assert steps["step-1"].status == "completed", "survivor keeps its status"
    assert steps["step-2"].status == "invalidated", "unproven step is reopened"
    assert "resume found execution behind" in (steps["step-2"].invalidated_reason or "")
    assert steps["step-3"].status == "pending"
    assert resumed.state.plan_history[-1].reason_category == "resume_reconciliation"
    actions = [e.payload.get("action") for e in events if e.event_type == "resume_reconciled"]
    assert "discarded_unrecorded_checkpoint" in actions
    assert resumed.state.status == "running", "reconciliation repaired, not blocked"


def test_invalidation_moves_verified_criteria_to_revalidation(tmp_path: Path) -> None:
    """W7: a verified criterion whose proof died with an invalidated step needs
    revalidation, not a silent verified claim and not a fresh unresolved one."""
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None and runtime.memory is not None
    from gcae.models import (
        CriterionResult,
        EvidenceKind,
        EvidenceRecord,
        PlanStep,
        VerificationReport,
    )

    proof = runtime.memory.add_evidence(
        EvidenceRecord(
            run_id=run_id, trajectory_step_id="step-2", kind=EvidenceKind.COMMAND_RESULT,
            claim_or_subject="file exists: out.txt",
        )
    )
    runtime.state.verified_criteria = ["file exists: out.txt"]
    runtime.state.last_verification = VerificationReport(
        passed=False,
        criteria=[
            CriterionResult(
                criterion="file exists: out.txt", passed=True, status="pass",
                evidence_ids=[proof.id or 0],
            )
        ],
    )
    runtime.state.plan = [
        PlanStep(id="step-1", goal="inspect", status="completed",
                 checkpoint=base, locked=True),
        PlanStep(id="step-2", goal="build", status="completed",
                 checkpoint="ffffffffffffffffffffffffffffffffffffffff", locked=True),
        PlanStep(id="step-3", goal="verify", status="pending"),
    ]
    runtime._reconcile_plan_with_rollback(base, "test rollback")
    assert runtime.state.verified_criteria == []
    assert runtime.state.revalidation_required == ["file exists: out.txt"]

    # the guardian treats revalidation-required as covered: the final gate re-checks it
    from gcae.guardian import Guardian

    guardian = Guardian()
    steps = [{"id": "step-3", "status": "pending"}]
    exempt = guardian.plan_health(
        steps=steps, history=[], version=1, current_step_id="step-3",
        accepted_commit=base, criteria=["file exists: out.txt"],
        verified_criteria=[], revalidation_required=["file exists: out.txt"],
    )
    assert exempt.ok
    strict = guardian.plan_health(
        steps=steps, history=[], version=1, current_step_id="step-3",
        accepted_commit=base, criteria=["file exists: out.txt"],
        verified_criteria=[], revalidation_required=[],
    )
    assert strict.kind == "missing_success_criterion"

    # passing final verification clears the revalidation need
    runtime._mark_criterion_verified("file exists: out.txt")
    assert runtime.state.revalidation_required == []
    assert "file exists: out.txt" in runtime.state.verified_criteria
