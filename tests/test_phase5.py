import logging
import subprocess
from pathlib import Path

from gcae.evaluator import LLMEvaluator
from gcae.models import Evaluation, EvaluationInput, MemoryRecord
from gcae.providers import FakeProvider
from gcae.runtime import Runtime


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class AlwaysAccept:
    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        del payload
        return Evaluation(decision="accept", reason="test", progress_score=1.0)


def test_runtime_finishes_with_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "answer.txt", "content": "ok"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, evaluator=AlwaysAccept())
    runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert Path(result.worktree, "answer.txt").read_text() == "ok"
    assert result.accepted_steps == 1


class KeepSpeculative:
    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        del payload
        return Evaluation(decision="continue", reason="keep speculative change")


def test_runtime_checkpoints_verified_speculative_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "answer.txt", "content": "ok"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
            {"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "done"},
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, evaluator=KeepSpeculative())
    state = runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    base = state.accepted_commit
    result = runtime.run()
    assert result.status == "complete"
    assert result.accepted_commit != base
    assert runtime.repo is not None
    assert runtime.repo.status() == ""
    assert (Path(result.worktree) / "answer.txt").read_text() == "ok"


def test_final_verification_blocks_premature_completion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [{"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "premature"}]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, max_steps=3)
    runtime.start("create missing", success_criteria=["file exists: missing.txt"])
    result = runtime.run()
    assert result.status != "complete"
    assert result.last_verification is not None
    assert not result.last_verification.passed


def test_llm_evaluator_promotes_memories(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "try implementation",
                "reason_summary": "bad attempt",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "bad.txt", "content": "bad"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "try implementation",
                "reason_summary": "step done",
            },
            {
                "decision": "rollback",
                "reason": "bad implementation",
                "progress_score": 0.0,
                "memories_to_promote": [
                    {
                        "record": {
                            "kind": "failure",
                            "content": "llm lesson: bad.txt is wrong",
                            "run_id": "model-supplied",
                        },
                        "score": 0.9,
                    }
                ],
            },
            {
                "action": "execute_tool",
                "semantic_goal": "correct implementation",
                "reason_summary": "correct attempt",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "result.txt", "content": "good"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "correct implementation",
                "reason_summary": "step done",
            },
            {"decision": "accept", "reason": "correct", "progress_score": 1.0},
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        evaluator=LLMEvaluator(provider),
        max_steps=6,
    )
    runtime.start("create result", success_criteria=["file exists: result.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert (Path(result.worktree) / "result.txt").read_text() == "good"
    assert not (Path(result.worktree) / "bad.txt").exists()
    promoted = [
        item
        for item in runtime.memory.all(result.run_id)
        if item.content == "llm lesson: bad.txt is wrong"
    ]
    assert promoted and promoted[0].source == "evaluator"
    assert promoted[0].run_id == result.run_id


def test_memory_is_shared_across_runs(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime_dir = tmp_path / "runtime"
    first = Runtime(source, runtime_dir, provider=FakeProvider([]))
    first.start("first request", run_id="run-a")
    assert first.memory is not None
    first.memory.add(
        MemoryRecord(
            kind="failure",
            content="cross-run lesson",
            run_id="run-a",
            immutable=True,
        )
    )
    second = Runtime(source, runtime_dir, provider=FakeProvider([]))
    second.start("second request", run_id="run-b")
    assert second.memory is not None
    found = second.memory.search("cross-run", 5)
    assert any(item.record.content == "cross-run lesson" for item in found)


def test_runtime_logs_lifecycle(tmp_path: Path, caplog) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "answer.txt", "content": "ok"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider)
    with caplog.at_level(logging.INFO, logger="gcae"):
        runtime.start("create answer", success_criteria=["file exists: answer.txt"])
        runtime.run()
    messages = [record.getMessage() for record in caplog.records if record.name == "gcae"]
    assert any("started" in message for message in messages)
    assert any("checkpoint" in message for message in messages)
    assert any("complete" in message for message in messages)
