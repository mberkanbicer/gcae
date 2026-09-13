import subprocess
from pathlib import Path

from gcae.models import Decision, Evaluation, ValidationResult
from gcae.runtime import Runtime


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class AlwaysAccept:
    def evaluate(self, decision: Decision, validation: ValidationResult) -> Evaluation:
        return Evaluation(
            outcome="accept",
            reason="test",
            progress=True,
            requirement_compliant=True,
            clean=True,
        )


def test_runtime_finishes_with_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = __import__("gcae.providers", fromlist=["FakeProvider"]).FakeProvider(
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
            {"action": "finish", "semantic_goal": "finish", "reason_summary": "done"},
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, evaluator=AlwaysAccept())
    runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert Path(result.worktree, "answer.txt").read_text() == "ok"
    assert result.accepted_steps == 1
