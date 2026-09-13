import subprocess
from pathlib import Path

from gcae.models import Evaluation, EvaluationInput
from gcae.providers import FakeProvider
from gcae.runtime import Runtime


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "app.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


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
                    "arguments": {"path": "bad.txt", "content": "bad"},
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
    observed_commits: list[str | None] = []

    class TrajectoryEvaluator:
        def evaluate(self, payload: EvaluationInput) -> Evaluation:
            del payload
            assert runtime.state is not None
            worktree = Path(runtime.state.worktree)
            observed_commits.append(runtime.state.accepted_commit)
            if (worktree / "bad.txt").exists():
                return Evaluation(decision="rollback", reason="bad implementation")
            candidate = worktree / "result.txt"
            if candidate.exists() and candidate.read_text() == "good":
                return Evaluation(
                    decision="accept",
                    reason="correct implementation",
                    progress_score=1.0,
                )
            return Evaluation(decision="rollback", reason="unexpected candidate")

    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        evaluator=TrajectoryEvaluator(),
        max_steps=10,
    )
    state = runtime.start("create result", success_criteria=["file exists: result.txt"])
    base = state.accepted_commit
    result = runtime.run()
    assert result.status == "complete"
    assert (source / "app.txt").read_text() == original
    assert not (source / "result.txt").exists()
    worktree = Path(result.worktree)
    assert (worktree / "result.txt").read_text() == "good"
    assert not (worktree / "bad.txt").exists()
    assert runtime.repo is not None
    assert runtime.repo.current_commit() == result.accepted_commit
    assert result.accepted_commit != base
    assert observed_commits and all(commit == base for commit in observed_commits)
    assert runtime.memory is not None
    assert any("bad implementation" in item.content for item in runtime.memory.all(result.run_id))
    run_dir = tmp_path / "runtime" / "runs" / result.run_id
    assert list((run_dir / "tool-results").glob("*.json"))
    assert list((run_dir / "diffs").glob("*.diff"))
