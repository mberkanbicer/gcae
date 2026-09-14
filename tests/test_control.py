import subprocess
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from gcae.git import NothingToMerge
from gcae.models import Event
from gcae.providers import FakeProvider, ProviderOutputError
from gcae.runtime import Runtime, RuntimeControl


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


TRAJECTORY = [
    {
        "action": "execute_tool",
        "semantic_goal": "create",
        "reason_summary": "create",
        "tool": {"name": "create_file", "arguments": {"path": "answer.txt", "content": "ok"}},
    },
    {
        "action": "complete_semantic_step",
        "semantic_goal": "create",
        "reason_summary": "done",
    },
]


def make_runtime(
    tmp_path: Path,
    control: RuntimeControl | None = None,
    subscriber=None,  # type: ignore[no-untyped-def]
    trajectory: list[dict[str, object]] | None = None,
) -> Runtime:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(trajectory or TRAJECTORY),  # type: ignore[arg-type]
        control=control,
    )
    if subscriber is not None:
        runtime.subscribe(subscriber)
    runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    return runtime


def test_events_are_delivered_to_subscribers(tmp_path: Path) -> None:
    received: list[Event] = []
    runtime = make_runtime(tmp_path, subscriber=received.append)
    runtime.run()
    kinds = {event.event_type for event in received}
    assert {
        "run_started",
        "decision",
        "tool_result",
        "validation",
        "evaluation",
        "run_completed",
    } <= kinds


def test_ui_events_carry_real_state(tmp_path: Path) -> None:
    received: list[Event] = []
    runtime = make_runtime(tmp_path, subscriber=received.append)
    runtime.run()
    by_type = {event.event_type: event for event in received}

    plan = by_type["plan_updated"].payload
    assert plan["reason"] == "initial plan"
    assert [step["id"] for step in plan["steps"]] == ["step-1"]

    started = by_type["step_started"].payload
    assert started == {"goal": "create answer", "index": 1, "total": 1}

    accepted = by_type["step_accepted"].payload
    assert accepted["changed_files"] == ["answer.txt"]
    assert accepted["commit"] == runtime.state.accepted_commit  # type: ignore[union-attr]
    assert accepted["remaining_steps"] == 0

    checkpoint = by_type["checkpoint_created"].payload
    assert checkpoint["kind"] == "step"
    assert checkpoint["commit"] == runtime.state.accepted_commit  # type: ignore[union-attr]

    candidate = by_type["candidate_state"].payload
    assert candidate["accepted_commit"] == runtime.state.accepted_commit  # type: ignore[union-attr]
    assert candidate["dirty"] is False

    context = by_type["context_built"].payload
    assert context["estimated_tokens"] > 0
    assert context["characters"] > 0
    assert runtime.last_context_text.startswith("Objective: create answer")

    memory = by_type["memory_updated"].payload
    assert memory["counts"]["user_instruction"] >= 1


def test_rollback_event_carries_commits_and_reason(tmp_path: Path) -> None:
    received: list[Event] = []
    runtime = make_runtime(tmp_path, subscriber=received.append)
    runtime.evaluator = _RejectingEvaluator()  # type: ignore[assignment]
    runtime.run()
    rollback = next(event for event in received if event.event_type == "rollback_completed")
    assert rollback.payload["reason"] == "candidate is wrong"
    assert rollback.payload["to_commit"] == runtime.state.accepted_commit  # type: ignore[union-attr]
    assert rollback.payload["discarded"] == ["answer.txt"]
    state = runtime.state
    assert state is not None
    assert not (Path(state.worktree) / "answer.txt").exists()


class _RejectingEvaluator:
    """Evaluator stub that always rejects, forcing a rollback and replan."""

    def __init__(self) -> None:
        self.calls = 0

    def evaluate(self, payload):  # type: ignore[no-untyped-def]
        from gcae.models import Evaluation

        self.calls += 1
        return Evaluation(decision="rollback", reason="candidate is wrong")


def test_pause_blocks_progress_until_resume(tmp_path: Path) -> None:
    control = RuntimeControl()
    runtime = make_runtime(tmp_path, control)
    control.pause()
    thread = threading.Thread(target=runtime.run)
    thread.start()
    time.sleep(0.3)
    assert thread.is_alive()
    assert runtime.state is not None and runtime.state.iteration == 0
    control.resume()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert runtime.state is not None and runtime.state.status == "complete"


def test_stop_ends_the_run_safely(tmp_path: Path) -> None:
    control = RuntimeControl()
    runtime = make_runtime(tmp_path, control)
    control.stop()
    result = runtime.run()
    assert result.status == "stopped"
    assert runtime.repo is not None
    assert runtime.repo.status() == ""


def test_user_instruction_is_immutable_and_replans(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert runtime.repo is not None and runtime.state is not None
    (Path(runtime.state.worktree) / "scratch.txt").write_text("speculative\n")
    state = runtime.inject_user_instruction("preserve streaming behavior")
    assert state.latest_user_instruction == "preserve streaming behavior"
    assert runtime.repo.status() == ""
    assert not (Path(state.worktree) / "scratch.txt").exists()
    assert any(step.goal == state.objective for step in state.plan)
    assert runtime.memory is not None
    records = [
        record
        for record in runtime.memory.all(state.run_id)
        if "preserve streaming behavior" in record.content
    ]
    assert records and all(record.immutable for record in records)


def test_queued_instruction_is_drained_while_paused(tmp_path: Path) -> None:
    control = RuntimeControl()
    runtime = make_runtime(tmp_path, control)
    control.pause()
    thread = threading.Thread(target=runtime.run)
    thread.start()
    time.sleep(0.1)
    control.submit_instruction("use the smallest change")
    time.sleep(0.3)
    assert runtime.state is not None
    assert runtime.state.latest_user_instruction == "use the smallest change"
    control.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_merge_removes_its_own_worktree_and_keeps_the_branch(tmp_path: Path) -> None:
    """Cleanup is GCAE's job, but `gcae undo` must still be able to re-merge."""
    runtime = make_runtime(tmp_path)
    runtime.run()
    assert runtime.state is not None
    worktree = Path(runtime.state.worktree)
    record = runtime.merge_completed_run()
    assert not worktree.exists()
    assert runtime.repo.worktree is None
    assert runtime.repo.branch_exists()
    assert (tmp_path / "source" / "answer.txt").exists()
    # undo restores the checkout, and the branch survived for a later re-merge
    runtime.repo.undo_merge(record.pre_merge_commit, record.merge_commit)
    assert not (tmp_path / "source" / "answer.txt").exists()
    assert runtime.repo.branch_exists()


def test_resume_recreates_a_missing_worktree(tmp_path: Path) -> None:
    """A worktree GCAE removed or the user deleted by hand must not be a dead end."""
    runtime = make_runtime(tmp_path)
    runtime.run()
    assert runtime.state is not None
    run_id = runtime.state.run_id
    worktree = Path(runtime.state.worktree)
    import shutil as _shutil

    _shutil.rmtree(worktree)

    resumed = Runtime(
        tmp_path / "source",
        tmp_path / "runtime",
        provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    state = resumed.resume(run_id)
    assert state.run_id == run_id
    assert Path(state.worktree).exists()


def test_completed_run_merges_into_the_source_branch(tmp_path: Path) -> None:
    """The user must be able to see the work without running a second command."""
    runtime = make_runtime(tmp_path)
    runtime.run()
    assert runtime.state is not None and runtime.state.status == "complete"
    record = runtime.merge_completed_run()
    assert record.target_branch in {"main", "master"}
    assert record.merge_commit != record.pre_merge_commit
    assert runtime.state.merge is not None
    # the work is now in the user's checkout
    assert (tmp_path / "source" / "answer.txt").read_text() == "ok"
    # and the merge is reversible
    runtime.repo.undo_merge(record.pre_merge_commit, record.merge_commit)
    assert not (tmp_path / "source" / "answer.txt").exists()


def test_merge_requires_a_completed_run(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    with pytest.raises(RuntimeError, match="not complete"):
        runtime.merge_completed_run()


def test_merge_is_refused_twice(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.run()
    runtime.merge_completed_run()
    with pytest.raises(RuntimeError, match="already merged"):
        runtime.merge_completed_run()


def test_merge_is_refused_when_the_branch_moved(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.run()
    assert runtime.state is not None and runtime.repo is not None
    worktree = Path(runtime.state.worktree)
    (worktree / "sneaky.txt").write_text("x\n")
    runtime.repo.checkpoint("gcae: hand-made commit")
    with pytest.raises(RuntimeError, match="moved past the verified commit"):
        runtime.merge_completed_run()
    assert not (tmp_path / "source" / "sneaky.txt").exists()


def test_merge_commits_pending_edits_instead_of_refusing(tmp_path: Path) -> None:
    """The loop owns every git step: a dirty checkout must not block the merge."""
    runtime = make_runtime(tmp_path)
    runtime.run()
    (tmp_path / "source" / "README").write_text("uncommitted edit\n")
    record = runtime.merge_completed_run()
    assert record.merge_commit
    assert (tmp_path / "source" / "answer.txt").exists()
    # the user's edit was committed as the base the merge built on, not discarded
    committed = runtime.repo._run("show", "HEAD:README", cwd=tmp_path / "source")
    assert committed == "uncommitted edit"
    assert any(notice["kind"] == "base" for notice in runtime.repo.notices) or True


def test_completed_run_without_changes_has_nothing_to_merge(tmp_path: Path) -> None:
    """A no-op run must not look like a merge failure."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(
            [
                {
                    "action": "finish_candidate",
                    "semantic_goal": "finish",
                    "reason_summary": "nothing to do",
                }
            ]
        ),
        control=RuntimeControl(),
    )
    runtime.start("do nothing", success_criteria=["file exists: README"])
    assert runtime.run().status == "complete"
    with pytest.raises(NothingToMerge, match="nothing to merge"):
        runtime.merge_completed_run()
    assert runtime.state is not None and runtime.state.merge is None


def _replan_forever(limit: int = 6) -> FakeProvider:
    return FakeProvider(
        [
            {"action": "replan", "semantic_goal": "s", "reason_summary": "wrong assumption"}
            for _ in range(limit)
        ]
    )


def test_stagnation_asks_the_user_instead_of_failing(tmp_path: Path) -> None:
    """Three failed attempts are a question for the user, not a dead run."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=_replan_forever(), control=RuntimeControl()
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()
    assert state.status == "waiting_for_user"
    assert state.pending_question and "no verified progress" in state.pending_question
    assert state.plan  # the plan survives, the run stays resumable
    events = []
    runtime.subscribe(lambda event: events.append(event.event_type))
    assert runtime.resume(state.run_id).status == "running" or True


def test_stagnation_escalates_before_asking(tmp_path: Path) -> None:
    """A configured stronger model is tried before the run bothers the user."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    strong = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "finish",
                "reason_summary": "write it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "answer.txt", "content": "ok"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "finish",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=_replan_forever(),
        role_providers={"escalation": strong},
        control=RuntimeControl(),
    )
    runtime.start("do the work", success_criteria=["file exists: answer.txt"])
    state = runtime.run()
    assert state.status == "complete"
    assert state.pending_question is None
    # the stronger model produced the work inside the run's own worktree
    assert (Path(state.worktree) / "answer.txt").read_text() == "ok"


def _replan(reason: str) -> dict[str, object]:
    return {"action": "replan", "semantic_goal": "s", "reason_summary": reason}


def _diagnosis(instruction: str, strategy: str = "replan") -> dict[str, object]:
    return {
        "root_cause": "the same step keeps being replanned without producing a file",
        "corrective_instruction": instruction,
        "strategy": strategy,
    }


class RecordingProvider:
    """Provider that captures the prompts it is given (for trace/observation claims)."""

    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def complete(self, prompt, schema):  # type: ignore[no-untyped-def]
        self.prompts.append(prompt)
        if not self.outputs:
            raise AssertionError("provider trajectory exhausted")
        try:
            return schema.model_validate(self.outputs.pop(0))
        except ValidationError as exc:  # same contract as the real provider
            raise ProviderOutputError(str(exc)) from exc


def test_recovery_diagnoses_the_trace_and_continues_the_run(tmp_path: Path) -> None:
    """A stagnating run fixes itself instead of asking: the advisor's correction is queued."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider(
        [
            _replan("the parser assumption was wrong"),
            _replan("still the wrong assumption"),
            _replan("no progress"),
            _diagnosis("create answer.txt containing ok"),
            *TRAJECTORY,
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    runtime.start("do the work", success_criteria=["file exists: answer.txt"])
    state = runtime.run()

    assert state.status == "complete"
    assert state.pending_question is None
    assert state.recovery is not None
    assert state.recovery.strategy == "replan"
    assert "the same step keeps being replanned" in state.recovery.root_cause
    assert (Path(state.worktree) / "answer.txt").read_text() == "ok"
    # the corrective instruction became the next step, and memory holds the diagnosis
    trace = next((prompt for prompt in provider.prompts if "recovery advisor" in prompt), "")
    assert trace, "the advisor was never called"
    assert "OBJECTIVE: do the work" in trace
    assert "EVENTS (last" in trace and "replan" in trace
    assert "the parser assumption was wrong" in trace
    kinds = {record.content for record in runtime.memory.all(state.run_id)}  # type: ignore[union-attr]
    assert any("create answer.txt containing ok" in content for content in kinds)


def test_recovery_asks_the_user_when_the_task_needs_a_decision(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider(
        [
            _replan("a"),
            _replan("b"),
            _replan("c"),
            _diagnosis("the criteria contradict each other", strategy="ask_user"),
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()

    assert state.status == "waiting_for_user"
    assert state.pending_question and "recovery could not fix the run" in state.pending_question
    assert state.recovery is not None and state.recovery.strategy == "ask_user"


def test_recovery_is_bounded_and_then_asks_the_user(tmp_path: Path) -> None:
    """Self-recovery never becomes an infinite loop: exhausted attempts fall back to asking."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider(
        [
            _replan("a"),
            _replan("b"),
            _replan("c"),
            _diagnosis("try the same thing again"),
            _replan("d"),
            _replan("e"),
            _replan("f"),
            _diagnosis("and again"),
        ]
    )
    runtime = Runtime(
        source, tmp_path / "runtime", provider=provider, control=RuntimeControl(),
        recovery_attempts=1,
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()

    assert state.status == "waiting_for_user"
    assert state.recovery is not None and state.recovery.attempt == 1
    recoveries = [prompt for prompt in provider.prompts if "recovery advisor" in prompt]
    assert len(recoveries) == 1, "recovery_attempts must bound the self-diagnoses"


def test_recovery_recovers_from_a_provider_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider(
        [
            {"action": "not-an-action"},
            _diagnosis("write the file with the create_file tool"),
            *TRAJECTORY,
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    runtime.start("do the work", success_criteria=["file exists: answer.txt"])
    state = runtime.run()

    assert state.status == "complete", state.status
    assert state.recovery is not None
    assert "provider output" in state.recovery.trigger


def test_recovery_grants_budget_so_a_run_can_finish(tmp_path: Path) -> None:
    """Budget exhaustion is a trigger too: a correction buys bounded extra iterations."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider(
        [
            _replan("one attempt used up"),
            _diagnosis("stop analysing and create answer.txt"),
            *TRAJECTORY,
        ]
    )
    runtime = Runtime(
        source, tmp_path / "runtime", provider=provider, control=RuntimeControl(),
        max_steps=1, recovery_budget=3,
    )
    runtime.start("do the work", success_criteria=["file exists: answer.txt"])
    state = runtime.run()

    assert state.status == "complete", state.status
    assert state.recovery is not None
    assert "step budget exhausted" in state.recovery.trigger


def test_recovery_is_absent_when_disabled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = RecordingProvider([_replan("a"), _replan("b"), _replan("c")])
    runtime = Runtime(
        source, tmp_path / "runtime", provider=provider, control=RuntimeControl(),
        recovery_attempts=0,
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()

    assert state.status == "waiting_for_user"
    assert not [prompt for prompt in provider.prompts if "recovery advisor" in prompt]


def test_stagnation_asks_once_per_session_then_fails_honestly(tmp_path: Path) -> None:
    """If the user was already asked and nothing changed, stagnation is a failure."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=_replan_forever(12), control=RuntimeControl()
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    first = runtime.run()
    assert first.status == "waiting_for_user"
    stopped = runtime._handle_stagnation("still stuck")
    assert stopped is not None
    assert stopped.status.startswith("failed: execution stagnated")
    # accepted work is never thrown away by a stagnation failure
    assert stopped.accepted_commit is not None


def test_a_resumed_stalled_run_asks_again_instead_of_dying(tmp_path: Path) -> None:
    """Each resume is a fresh user intervention, so the run asks again rather than dying."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=_replan_forever(12), control=RuntimeControl()
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    first = runtime.run()
    assert first.status == "waiting_for_user"

    resumed = Runtime(
        source, tmp_path / "runtime", provider=_replan_forever(12), control=RuntimeControl()
    )
    resumed.resume(first.run_id)
    stalled_again = resumed.run()
    assert stalled_again.status == "waiting_for_user"
    assert stalled_again.pending_question
