import asyncio
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gcae.models import Event
from gcae.planner import LLMPlanner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl
from gcae.tui.app import DiffScreen, GcaeApp, RequestScreen


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


def make_runtime(tmp_path: Path, trajectory: list[dict[str, object]] | None = None) -> Runtime:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(trajectory or TRAJECTORY),
        control=RuntimeControl(),
    )
    runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    return runtime


def test_dashboard_renders_and_completes(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(200):
                if app.agent_done:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            assert app.agent_done
            assert runtime.state is not None
            assert runtime.state.status == "complete"
            assert "create answer" in app.panel_state["objective"]
            assert "context limit" in app.panel_state["context"]
            assert "model:" in app.panel_state["model"]
            assert "accepted steps:" in app.panel_state["git"]
            assert any("complete" in line for line in app.log_lines)

    asyncio.run(scenario())


def test_pause_resume_and_stop_keys(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("p")
            assert app.control.paused
            await pilot.press("r")
            assert not app.control.paused
            await pilot.press("s")
            assert app.control.stopped

    asyncio.run(scenario())


def test_tui_prompts_for_request_and_infers_criteria(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    plan = {
        "objective": "build the answer file",
        "success_criteria": ["file exists: answer.txt"],
        "hard_constraints": [],
        "assumptions": [],
        "steps": [
            {
                "id": "step-1",
                "goal": "create answer.txt",
                "rationale": "requested",
                "expected_result": "file exists",
                "intended_scope": [],
                "validation_requirements": [],
            }
        ],
    }
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(TRAJECTORY),
        planner=LLMPlanner(FakeProvider([plan])),
        control=RuntimeControl(),
    )
    app = GcaeApp(runtime)  # no request, no state: the TUI must ask for the task

    async def scenario() -> None:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RequestScreen)
            for character in "build":
                await pilot.press(character)
            await pilot.press("enter")
            for _ in range(200):
                if app.agent_done:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            assert app.agent_done
            assert runtime.state is not None
            assert runtime.state.status == "complete"
            assert runtime.state.objective == "build the answer file"
            assert runtime.state.success_criteria == ["file exists: answer.txt"]
            assert "build the answer file" in app.panel_state["objective"]

    asyncio.run(scenario())


def test_user_override_submission(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("i")
            await pilot.pause()
            for character in "preserve":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            assert app.control.take_instructions() == ["preserve"]

    asyncio.run(scenario())


def test_diff_view_opens_and_closes(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert runtime.state is not None
    (Path(runtime.state.worktree) / "README").write_text("modified\n")
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test() as pilot:
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, DiffScreen)
            assert "modified" in app.screen.diff
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DiffScreen)

    asyncio.run(scenario())


def test_runtime_events_update_panels(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test():
            app._on_event(
                Event(
                    run_id="r",
                    event_type="validation",
                    payload={
                        "passed": True,
                        "diff_check_passed": True,
                        "command_results": [],
                        "changed_files": ["answer.txt"],
                        "warnings": [],
                    },
                )
            )
            assert "passed: True" in app.panel_state["validation"]
            app._on_event(
                Event(
                    run_id="r",
                    event_type="evaluation",
                    payload={"decision": "rollback", "reason": "bad implementation"},
                )
            )
            assert "rollback" in app.last_evaluation
            assert any("rollback" in line for line in app.log_lines)

    asyncio.run(scenario())
