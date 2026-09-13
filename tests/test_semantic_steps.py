import subprocess
from pathlib import Path

from gcae.models import Evaluation, EvaluationInput, ToolCall
from gcae.providers import FakeProvider
from gcae.runtime import Runtime
from gcae.tools import ToolRegistry


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class CountingEvaluator:
    def __init__(self, decisions: list[str] | None = None) -> None:
        self.calls = 0
        self.decisions = decisions or []

    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        self.calls += 1
        if self.decisions:
            decision = self.decisions.pop(0)
            return Evaluation(decision=decision, reason=f"call {self.calls}")
        return Evaluation(decision="accept", reason="accepted", progress_score=1.0)


def tool(action: str, name: str, **arguments: object) -> dict[str, object]:
    return {
        "action": action,
        "semantic_goal": "step",
        "reason_summary": "step",
        "tool": {"name": name, "arguments": arguments},
    }


def test_tools_run_until_step_completion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    evaluator = CountingEvaluator()
    provider = FakeProvider(
        [
            tool("execute_tool", "read_file", path="README"),
            tool("execute_tool", "list_files"),
            {
                "action": "complete_semantic_step",
                "semantic_goal": "inspect",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, evaluator=evaluator)
    runtime.start("inspect the repository", success_criteria=["file exists: README"])
    result = runtime.run()
    assert result.status == "complete"
    assert evaluator.calls == 1  # one evaluation for the whole semantic step
    assert result.latest_observations
    assert result.step_tool_calls == 2


def test_step_tool_budget_forces_evaluation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    evaluator = CountingEvaluator()
    provider = FakeProvider(
        [
            tool("execute_tool", "create_file", path="a.txt", content="a"),
            tool("execute_tool", "create_file", path="b.txt", content="b"),
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        evaluator=evaluator,
        max_tool_calls_per_step=2,
    )
    runtime.start("create files", success_criteria=["file exists: b.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert evaluator.calls == 1
    assert result.accepted_steps == 1
    assert (Path(result.worktree) / "b.txt").exists()


def test_read_only_step_completes_without_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            tool("execute_tool", "list_files"),
            {
                "action": "complete_semantic_step",
                "semantic_goal": "inspect",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider)
    state = runtime.start("inspect the repository", success_criteria=["file exists: README"])
    base = state.accepted_commit
    result = runtime.run()
    assert result.status == "complete"
    assert result.accepted_steps == 0
    assert result.accepted_commit == base
    assert runtime.repo is not None
    assert runtime.repo.status() == ""


def test_speculative_changes_are_discarded_on_replan(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            tool("execute_tool", "create_file", path="scratch.txt", content="x"),
            {"action": "replan", "semantic_goal": "rethink", "reason_summary": "wrong route"},
            tool("execute_tool", "create_file", path="result.txt", content="ok"),
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, max_steps=8)
    runtime.start("create result", success_criteria=["file exists: result.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert (Path(result.worktree) / "result.txt").exists()
    assert not (Path(result.worktree) / "scratch.txt").exists()
    assert result.accepted_steps == 1


def test_write_file_overwrites_and_run_tests_executes(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.txt").write_text("old\n")
    tools = ToolRegistry(
        worktree,
        test_commands=["test -f a.txt"],
        artifact_dir=tmp_path / "artifacts",
    )
    assert tools.write_file({"path": "a.txt", "content": "new\n"}).success
    assert (worktree / "a.txt").read_text() == "new\n"
    result = tools.run_tests({})
    assert result.success
    assert "$ test -f a.txt" in result.output


def test_large_output_is_stored_as_artifact(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    artifacts = tmp_path / "artifacts"
    tools = ToolRegistry(worktree, artifact_dir=artifacts, max_output_chars=100)
    result = tools.execute(
        ToolCall(
            name="run_command",
            arguments={"command": "python -c \"print('x' * 5000)\""},
        )
    )
    assert result.success
    assert result.artifact is not None
    assert Path(result.artifact).is_file()
    assert result.artifact in result.output
    assert len(result.output) < 5000
