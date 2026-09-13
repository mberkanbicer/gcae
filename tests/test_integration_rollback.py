import subprocess
from pathlib import Path

from gcae.models import Decision, Evaluation, ValidationResult
from gcae.providers import FakeProvider
from gcae.runtime import Runtime


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "app.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class RejectBadAcceptGood:
    def evaluate(self, decision: Decision, validation: ValidationResult) -> Evaluation:
        del decision
        content = Path(validation.changed_files[0]).name if validation.changed_files else ""
        del content
        candidate = validation.command_results
        del candidate
        return Evaluation(outcome="accept", reason="default")


def test_end_to_end_rollback_trajectory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    original = (source / "app.txt").read_text()
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "try implementation",
                "reason_summary": "bad first attempt",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "result.txt", "content": "bad"},
                },
            },
            {
                "action": "execute_tool",
                "semantic_goal": "correct implementation",
                "reason_summary": "correct second attempt",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "result.txt", "content": "good"},
                },
            },
            {"action": "finish", "semantic_goal": "finish", "reason_summary": "verified"},
        ]
    )

    class TrajectoryEvaluator:
        def evaluate(self, decision: Decision, validation: ValidationResult) -> Evaluation:
            del decision
            candidate = (
                Path(runtime.state.worktree) / validation.changed_files[0]
                if validation.changed_files and runtime.state is not None
                else None
            )
            if candidate and candidate.name == "result.txt":
                text = candidate.read_text()
                if text == "bad":
                    return Evaluation(outcome="rollback", reason="bad implementation")
                if text == "good":
                    return Evaluation(
                        outcome="accept",
                        reason="correct implementation",
                        progress=True,
                        requirement_compliant=True,
                        clean=True,
                    )
            return Evaluation(outcome="rollback", reason="unexpected candidate")

    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        evaluator=TrajectoryEvaluator(),
        max_steps=10,
    )
    runtime.start("create result", success_criteria=["file exists: result.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert (source / "app.txt").read_text() == original
    assert (Path(result.worktree) / "result.txt").read_text() == "good"
    assert runtime.repo is not None
    assert runtime.repo.current_commit() == result.accepted_commit
    assert not (Path(result.worktree) / "bad.txt").exists()
    assert runtime.memory is not None
    assert any("bad implementation" in item.content for item in runtime.memory.all(result.run_id))
    assert not (source / "result.txt").exists()
