import subprocess
from pathlib import Path

from gcae.context import ContextBuilder, estimate_tokens
from gcae.memory import MemoryStore
from gcae.models import AgentState, MemoryRecord, SemanticStep
from gcae.providers import FakeProvider
from gcae.runtime import Runtime


def state() -> AgentState:
    return AgentState(
        run_id="r",
        source_repo="/s",
        worktree="/w",
        branch="b",
        objective="python",
        original_request="python",
        hard_constraints=["no new dependencies"],
        success_criteria=["file exists: result.txt"],
    )


def test_context_budget_prefers_pinned_over_low_priority(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    for index in range(40):
        store.add(
            MemoryRecord(
                kind="observation",
                content=f"python old log line {index} " + "x" * 80,
                run_id="r",
            )
        )
    lesson = store.add(
        MemoryRecord(
            kind="failure",
            content="critical failure lesson: never replace the streaming parser",
            run_id="r",
            immutable=True,
        )
    )
    store.add(
        MemoryRecord(
            kind="user_instruction",
            content="original request: python",
            run_id="r",
            immutable=True,
        )
    )
    context = ContextBuilder(store).build(
        state(), SemanticStep(id="s", goal="write result"), budget=300
    )
    assert "Objective: python" in context.text
    assert "no new dependencies" in context.text
    assert "file exists: result.txt" in context.text
    assert "write result" in context.text
    assert "critical failure lesson" in context.text
    assert lesson.id in context.pinned_ids
    assert context.omitted_ids
    assert estimate_tokens(context.text) <= 300
    dropped = store.get(context.omitted_ids[0])
    assert dropped.content not in context.text
    assert store.get(dropped.id or 0).content == dropped.content
    store.close()


def test_long_trajectory_keeps_records_after_budget_trim(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "T"], check=True)
    (source / "README").write_text("base\n")
    for index in range(10):
        (source / f"file{index}.txt").write_text(f"content {index}\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)

    trajectory: list[dict[str, object]] = []
    for index in range(10):
        trajectory.append(
            {
                "action": "execute_tool",
                "semantic_goal": "inspect",
                "reason_summary": "inspect",
                "tool": {"name": "read_file", "arguments": {"path": f"file{index}.txt"}},
            }
        )
    trajectory.append(
        {
            "action": "complete_semantic_step",
            "semantic_goal": "inspect",
            "reason_summary": "done",
        }
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(trajectory),
        max_tool_calls_per_step=10,
    )
    runtime.start(
        "inspect the repository",
        hard_constraints=["no new dependencies"],
        success_criteria=["file exists: README"],
    )
    result = runtime.run()
    assert result.status == "complete"
    assert runtime.memory is not None
    records = runtime.memory.all(result.run_id)
    assert len([item for item in records if item.kind == "observation"]) >= 10
    for index in range(30):
        runtime.memory.add(
            MemoryRecord(
                kind="observation",
                content=f"inspect repository noise {index} " + "y" * 60,
                run_id=result.run_id,
            )
        )
    before = len(runtime.memory.all(result.run_id))
    context = ContextBuilder(runtime.memory).build(
        result,
        SemanticStep(id="s2", goal="next"),
        budget=250,
    )
    assert "Objective: inspect the repository" in context.text
    assert "no new dependencies" in context.text
    assert "file exists: README" in context.text
    assert context.omitted_ids
    assert len(runtime.memory.all(result.run_id)) == before
