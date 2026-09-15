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


def completed_run(tmp_path: Path) -> tuple[object, str]:
    """A fully completed run with an accepted checkpoint, unmerged."""

    from gcae.models import AgentState
    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    source = tmp_path / "source"
    init_repo(source)
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
    state = runtime.run()
    assert isinstance(state, AgentState) and state.status == "complete", state.status
    return runtime, state.run_id


def test_merge_crash_window_backfills_the_record(tmp_path: Path) -> None:
    """W2: crash after `git merge`, before the record write — undo must still work."""
    from gcae.git import GitRepository
    from gcae.models import PendingMerge
    from gcae.persistence import StateStore
    from gcae.runtime import merge_verified_run

    runtime, run_id = completed_run(tmp_path)
    state = runtime.state
    assert state is not None and state.branch
    state_path = tmp_path / "runtime" / "runs" / run_id / "state.json"
    repo = GitRepository(str(tmp_path / "source"), tmp_path / "runtime")
    pre = repo.source_commit()
    # the merge ran but the process died before state.merge was persisted
    _, merged = repo.merge_branch(state.branch)
    state.pending_merge = PendingMerge(
        branch=state.branch, target_branch=repo.current_branch(),
        pre_merge_commit=pre,
    )
    StateStore(state_path).save(state)
    head_after_crash = repo.source_commit()
    record = merge_verified_run(
        repo, state, persist=lambda: StateStore(state_path).save(state)
    )
    assert record.merge_commit == merged == head_after_crash, "no second merge"
    assert record.pre_merge_commit == pre
    assert state.pending_merge is None and state.merge is record
    # undo reverses exactly the recorded merge
    repo.undo_merge(record.pre_merge_commit, record.merge_commit)
    assert repo.source_commit() == pre


def test_merge_marker_without_merge_completes_once(tmp_path: Path) -> None:
    """Marker persisted, crash before the merge: the next attempt merges and clears."""
    from gcae.git import GitRepository
    from gcae.models import PendingMerge
    from gcae.persistence import StateStore
    from gcae.runtime import merge_verified_run

    runtime, run_id = completed_run(tmp_path)
    state = runtime.state
    assert state is not None and state.branch
    state_path = tmp_path / "runtime" / "runs" / run_id / "state.json"
    repo = GitRepository(str(tmp_path / "source"), tmp_path / "runtime")
    pre = repo.source_commit()
    state.pending_merge = PendingMerge(
        branch=state.branch, target_branch=repo.current_branch(),
        pre_merge_commit=pre,
    )
    StateStore(state_path).save(state)
    record = merge_verified_run(
        repo, state, persist=lambda: StateStore(state_path).save(state)
    )
    assert state.merge is record and state.pending_merge is None
    assert repo.source_commit() == record.merge_commit != pre
    again = StateStore(state_path).load()
    assert again.merge is not None
    import pytest

    from gcae.runtime import merge_verified_run as mvr

    with pytest.raises(RuntimeError, match="already merged"):
        mvr(repo, again, persist=lambda: StateStore(state_path).save(again))


def test_blocked_run_stays_blocked_across_resume(tmp_path: Path) -> None:
    """W4: a plain resume of a blocked run holds it — no silent autonomous restart."""
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    runtime.state.status = "blocked"
    runtime.state.blocked_reason = "credentials needed"
    runtime.state.unblock_hint = "set OPENROUTER_API_KEY"
    runtime._persist()
    resumed, events = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    assert resumed.state.status == "blocked"
    assert resumed.state.blocked_reason == "credentials needed"
    assert resumed.state.unblock_hint == "set OPENROUTER_API_KEY"
    held = [e for e in events if e.event_type == "run_resumed"]
    assert held and held[0].payload.get("held") == "blocked"
    # and run() does not start working behind the block
    out = resumed.run()
    assert out.status == "blocked"


def test_force_resume_overrides_the_block_openly(tmp_path: Path) -> None:
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    runtime.state.status = "blocked"
    runtime.state.blocked_reason = "credentials needed"
    runtime._persist()
    forced = Runtime(
        tmp_path / "source", tmp_path / "runtime", provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    events: list = []
    forced.subscribe(events.append)
    forced.resume(run_id, force=True)
    assert forced.state is not None
    assert forced.state.status == "running"
    assert forced.state.blocked_reason is None
    assert any(e.event_type == "resume_forced" for e in events)


def test_instruction_answers_a_held_run(tmp_path: Path) -> None:
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    runtime.state.status = "blocked"
    runtime.state.blocked_reason = "pick a strategy"
    runtime._persist()
    resumed, _ = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    resumed.inject_user_instruction("use the PTY execution strategy")
    assert resumed.state.status == "running"
    assert resumed.state.blocked_reason is None


def test_waiting_run_keeps_its_question_across_resume(tmp_path: Path) -> None:
    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    runtime.state.status = "waiting_for_user"
    runtime.state.pending_question = "which database should the app target?"
    runtime._persist()
    resumed, _ = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    assert resumed.state.status == "waiting_for_user"
    assert resumed.state.pending_question == "which database should the app target?"


_CHILD_SCRIPT = '''
import sys
from pathlib import Path

from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl

mode = sys.argv[1]
source, runtime_dir = Path(sys.argv[2]), Path(sys.argv[3])
run_id_file, marker = Path(sys.argv[4]), Path(sys.argv[5])

if mode == "run":
    provider = FakeProvider([
        {"action": "execute_tool", "semantic_goal": "create", "reason_summary": "create",
         "tool": {"name": "create_file",
                  "arguments": {"path": "out.txt", "content": "ok\\n"}}},
        {"action": "complete_semantic_step", "semantic_goal": "create",
         "reason_summary": "done"},
        {"action": "execute_tool", "semantic_goal": "hold",
         "reason_summary": "hold",
         "tool": {"name": "run_command", "arguments": {"command": "sleep 30"}}},
    ])
    runtime = Runtime(source, runtime_dir, provider=provider, control=RuntimeControl())
    seen = []

    def watch(event) -> None:
        if event.event_type == "checkpoint_created":
            marker.write_text(event.payload.get("commit") or "?", encoding="utf-8")

    runtime.subscribe(watch)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    run_id_file.write_text(runtime.state.run_id, encoding="utf-8")
    runtime.run()
    import time

    time.sleep(600)  # hold: the parent must be able to kill a live process
else:
    run_id = run_id_file.read_text(encoding="utf-8").strip()
    # ensure out.txt once (a no-op if the checkpoint survived), then cycles whose
    # write always changes the tree — a fixed-content repeat can burn decisions
    # as a rejected no-op until the provider exhausts on slow machines
    ensure = [
        {"action": "execute_tool", "semantic_goal": "ensure out", "reason_summary": "ensure",
         "tool": {"name": "write_file",
                  "arguments": {"path": "out.txt", "content": "ok\\n"}}},
        {"action": "complete_semantic_step", "semantic_goal": "ensure out",
         "reason_summary": "done"},
    ]
    cycles = []
    for i in range(10):
        cycles.extend([
            {"action": "execute_tool", "semantic_goal": "finish", "reason_summary": "finish",
             "tool": {"name": "write_file",
                      "arguments": {"path": "done.txt", "content": f"done {i}\\n"}}},
            {"action": "complete_semantic_step", "semantic_goal": "finish",
             "reason_summary": "done"},
            {"action": "finish_candidate", "semantic_goal": "finish",
             "reason_summary": "finish"},
        ])
    provider = FakeProvider(ensure + cycles)
    runtime = Runtime(source, runtime_dir, provider=provider, control=RuntimeControl())
    runtime.resume(run_id)
    state = runtime.run()
    marker.write_text(state.status, encoding="utf-8")
'''


def test_sigkill_mid_run_resumes_and_completes(tmp_path: Path) -> None:
    """The real crash: SIGKILL after the first checkpoint, resume in a new process."""
    import os
    import signal
    import subprocess
    import sys
    import time

    source = tmp_path / "source"
    init_repo(source)
    runtime_dir = tmp_path / "runtime"
    child = tmp_path / "child.py"
    child.write_text(_CHILD_SCRIPT, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    run_id_file = tmp_path / "run_id"
    marker = tmp_path / "checkpoint_seen"

    first = subprocess.Popen(
        [sys.executable, str(child), "run", str(source), str(runtime_dir),
         str(run_id_file), str(marker)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 60
    while not marker.exists() and first.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the child never reached its first checkpoint"
    first.send_signal(signal.SIGKILL)
    first.wait(timeout=10)
    assert first.returncode == -signal.SIGKILL
    run_id = run_id_file.read_text(encoding="utf-8").strip()

    status_marker = tmp_path / "resume_status"
    second = subprocess.run(
        [sys.executable, str(child), "resume", str(source), str(runtime_dir),
         str(run_id_file), str(status_marker)],
        env=env, capture_output=True, text=True, timeout=180,
    )
    assert second.returncode == 0, second.stderr[-2000:]
    assert status_marker.read_text(encoding="utf-8").strip() == "complete"
    events_log = runtime_dir / "runs" / run_id / "events.jsonl"
    lines = events_log.read_text(encoding="utf-8").splitlines()
    assert lines and all(json.loads(line) is not None for line in lines)
    kinds = [json.loads(line)["event_type"] for line in lines]
    assert "run_resumed" in kinds, "the resumed process must say it resumed"
    assert not (runtime_dir / "runs" / run_id / "state.json.write").exists()


def test_crash_between_accept_and_next_step_queues_the_followup(tmp_path: Path) -> None:
    """Kill landed after the acceptance persist but before the next step was queued:
    current_step_id points at a completed step with nothing executable. Resume must
    queue exactly one follow-up step instead of blocking as corrupted."""
    from gcae.models import PlanStep

    runtime, run_id, base = start_runtime(tmp_path)
    assert runtime.state is not None
    runtime.state.plan = [
        PlanStep(id="step-1", goal="create", status="completed",
                 checkpoint=base, locked=True),
    ]
    runtime.state.current_step_id = "step-1"
    runtime.state.accepted_commit = base
    runtime._persist()
    resumed, _ = resumed_runtime(tmp_path, run_id)
    assert resumed.state is not None
    assert resumed.state.status == "running", "repaired, not blocked"
    current = next(
        (s for s in resumed.state.plan if s.id == resumed.state.current_step_id), None
    )
    assert current is not None and current.status in {"pending", "active"}
    assert current.id != "step-1", "a follow-up step was queued"
    assert resumed.state.plan[0].status == "completed", "history untouched"
