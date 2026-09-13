import subprocess
import threading
import time
from pathlib import Path

from gcae.models import Event
from gcae.providers import FakeProvider
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
