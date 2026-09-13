"""Dashboard tests: rendering, event updates, interaction, responsive layout."""

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

pytest.importorskip("textual")

from gcae.models import Event, RunPhase
from gcae.planner import LLMPlanner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl
from gcae.tui import formatters
from gcae.tui.app import GcaeApp
from gcae.tui.modals import ConfirmStopModal, HelpModal, InstructionModal, RequestModal
from gcae.tui.screens import (
    ContextScreen,
    DiffScreen,
    EvaluationScreen,
    LogsScreen,
    MemoryScreen,
    PlanScreen,
)
from gcae.tui.state import UiState
from gcae.tui.widgets import (
    ActivityPanel,
    BannerPanel,
    CheckpointPanel,
    FooterBar,
    MetricsPanel,
    ObjectivePanel,
    PlanPanel,
    StatusBar,
    TimelinePanel,
    ValidationPanel,
)


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
        "reason_summary": "create the answer",
        "expected_result": "answer.txt exists",
        "tool": {"name": "create_file", "arguments": {"path": "answer.txt", "content": "ok"}},
    },
    {
        "action": "complete_semantic_step",
        "semantic_goal": "create",
        "reason_summary": "done",
    },
]

PLAN = {
    "objective": "create answer",
    "success_criteria": ["file exists: answer.txt"],
    "hard_constraints": [],
    "assumptions": [],
    "steps": [
        {
            "id": "step-1",
            "goal": "create answer.txt",
            "rationale": "requested",
            "expected_result": "answer.txt exists",
            "intended_scope": ["answer.txt"],
            "validation_requirements": [],
        }
    ],
}

MULTI_PLAN = {
    "objective": "fix the parser",
    "success_criteria": ["file exists: answer.txt"],
    "hard_constraints": [],
    "assumptions": [],
    "steps": [
        {
            "id": "step-1",
            "goal": "inspect the parser",
            "rationale": "understand the failure",
            "expected_result": "known failure mode",
            "intended_scope": [],
            "validation_requirements": [],
        },
        {
            "id": "step-2",
            "goal": "fix quote handling",
            "rationale": "root cause",
            "expected_result": "quotes preserved",
            "intended_scope": ["src/parser.py"],
            "validation_requirements": ["command succeeds: pytest"],
        },
    ],
}


def make_runtime(
    tmp_path: Path,
    *,
    trajectory: list[dict[str, object]] | None = None,
    plan: dict[str, object] | None = None,
    start: bool = True,
) -> Runtime:
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    if not (source / ".git").exists():
        init_repo(source)
    provider = FakeProvider(trajectory or TRAJECTORY)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=LLMPlanner(FakeProvider([plan or PLAN])),
        control=RuntimeControl(),
        provider_label="FAKE",
    )
    if start:
        runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    return runtime


def event(event_type: str, payload: dict[str, object] | None = None, **kwargs: object) -> Event:
    return Event(
        run_id="r",
        event_type=event_type,
        payload=payload or {},
        timestamp=datetime.now(UTC),
        **kwargs,  # type: ignore[arg-type]
    )


async def wait_for(pilot, predicate, attempts: int = 300) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await pilot.pause(0.05)
    raise AssertionError("condition was not reached in time")


# --------------------------------------------------------------------- rendering


def test_dashboard_renders_while_running_and_completes(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(140, 45)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            await pilot.pause(0.3)
            assert runtime.state is not None
            assert runtime.state.status == "complete"
            assert app.ui.plan == []
            assert app.ui.completed_steps >= 1
            objective = str(app.query_one(ObjectivePanel).body.plain)
            assert "create answer" in objective
            checkpoint = str(app.query_one(CheckpointPanel).body.plain)
            assert "TRUSTED" in checkpoint.upper()
            assert runtime.state.accepted_commit[:7] in checkpoint
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN COMPLETE" in banner
            assert "1 accepted step" in banner
            timeline = str(app.query_one(TimelinePanel).body.plain)
            assert "checkpoint" in timeline
            assert "step accepted" in timeline

    asyncio.run(scenario())


def test_empty_state_renders_without_a_task(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path, start=False)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RequestModal)
            assert "no task yet" in str(app.query_one(ObjectivePanel).body.plain)
            assert "not run yet" in str(app.query_one(ValidationPanel).body.plain)

    asyncio.run(scenario())


def test_completed_and_failed_states_render(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            assert runtime.state is not None
            runtime.state.status = "complete"
            app.ui.agent_done = True
            app._refresh_panels({"banner", "status", "footer", "activity"})
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN COMPLETE" in banner
            assert "branch gcae/" in banner
            status = str(app.query_one(StatusBar).body.plain)
            assert "COMPLETE" in status

            runtime.state.status = "failed: provider output"
            app.ui.last_error = "provider request failed: connection refused"
            app.ui.action = None
            app._refresh_panels({"banner", "status", "activity", "footer"})
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN FAILED" in banner
            assert "connection refused" in banner
            assert "accepted work is safe" in banner
            assert "press i to describe a new task" in banner

    asyncio.run(scenario())


def test_paused_state_is_visible_and_actionable(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert app.control.paused
            status = str(app.query_one(StatusBar).body.plain)
            assert "PAUSED" in status
            footer = str(app.query_one(FooterBar).body.plain)
            assert "[r] Resume" in footer
            assert "[p] Pause" not in footer
            await pilot.press("r")
            await pilot.pause()
            assert not app.control.paused
            assert "[p] Pause" in str(app.query_one(FooterBar).body.plain)

    asyncio.run(scenario())


# ------------------------------------------------------------------ event updates


def test_plan_events_update_the_plan_panel(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "plan_updated",
                    {
                        "reason": "initial plan",
                        "steps": [
                            {"id": "step-2", "goal": "fix quote handling", "status": "active"},
                            {"id": "step-3", "goal": "run regression tests", "status": "pending"},
                        ],
                        "completed": 1,
                    },
                )
            )
            plan = str(app.query_one(PlanPanel).body.plain)
            assert "1/3 steps" in plan
            assert "● fix quote handling" in plan
            assert "○ run regression tests" in plan

            app._consume_event(
                event("replan", {"reason": "parser ownership assumption invalid"})
            )
            timeline = str(app.query_one(TimelinePanel).body.plain)
            assert "replan" in timeline
            assert "parser ownership assumption invalid" in timeline

    asyncio.run(scenario())


def test_step_and_tool_events_update_activity(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "step_started",
                    {"goal": "fix quote state across chunks", "index": 2, "total": 5},
                    phase=RunPhase.EXECUTE,
                    step_id="step-2",
                )
            )
            objective = str(app.query_one(ObjectivePanel).body.plain)
            assert "NOW" in objective
            assert "fix quote state across chunks" in objective
            activity = str(app.query_one(ActivityPanel).body.plain)
            assert "waiting for model" in activity
            # a model call in flight shows the role and how long it has been waiting
            assert "act · " in activity

            app._consume_event(
                event(
                    "decision",
                    {
                        "action": "execute_tool",
                        "reason_summary": "run the parser tests",
                        "expected_result": "quote state preserved",
                        "tool": {
                            "name": "run_command",
                            "arguments": {"command": "pytest tests/test_parser.py", "timeout": 60},
                        },
                    },
                    phase=RunPhase.EXECUTE,
                    step_id="step-2",
                )
            )
            activity = str(app.query_one(ActivityPanel).body.plain)
            assert "pytest tests/test_parser.py" in activity
            assert "RUNNING" in activity
            assert '{"command"' not in activity

            app._consume_event(
                event(
                    "tool_result",
                    {
                        "tool": "run_command",
                        "success": False,
                        "exit_code": 1,
                        "duration_ms": 1840.0,
                        "error": "2 failed, 10 passed",
                    },
                    phase=RunPhase.EXECUTE,
                )
            )
            activity = str(app.query_one(ActivityPanel).body.plain)
            assert "FAILED" in activity
            assert "exit 1" in activity
            assert "2 failed, 10 passed" in activity
            timeline = str(app.query_one(TimelinePanel).body.plain)
            assert "run_command failed" in timeline

    asyncio.run(scenario())


def test_validation_and_verification_update_the_panel(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "validation",
                    {
                        "passed": False,
                        "commands": ["pytest tests/parser/", "ruff check ."],
                        "command_results": [
                            {
                                "tool": "run_command",
                                "success": False,
                                "exit_code": 1,
                                "duration_ms": 4200.0,
                            },
                            {"tool": "run_command", "success": True, "exit_code": 0},
                        ],
                        "diff_check_passed": True,
                        "changed_files": ["src/parser.py"],
                        "warnings": ["dependency manifests changed: pyproject.toml"],
                    },
                )
            )
            panel = str(app.query_one(ValidationPanel).body.plain)
            assert "FAIL" in panel
            assert "pytest tests/parser/" in panel
            assert "ruff check ." in panel
            assert "git diff --check" in panel
            assert "dependency manifests changed" in panel

            app._consume_event(
                event(
                    "verification_completed",
                    {
                        "passed": False,
                        "criteria": [
                            {
                                "criterion": "file exists: answer.txt",
                                "passed": True,
                                "evidence": "",
                            },
                            {
                                "criterion": "command succeeds: pytest",
                                "passed": False,
                                "evidence": "2 failed, 10 passed",
                            },
                        ],
                    },
                )
            )
            panel = str(app.query_one(ValidationPanel).body.plain)
            assert "FAILED" in panel
            assert "2 failed, 10 passed" in panel

    asyncio.run(scenario())


def test_checkpoint_and_candidate_events_update_git_panel(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "candidate_state",
                    {
                        "dirty": True,
                        "added": 34,
                        "deleted": 11,
                        "files": [
                            {"code": " M", "path": "src/parser.py", "added": 24, "deleted": 5},
                            {
                                "code": " M",
                                "path": "tests/test_parser.py",
                                "added": 10,
                                "deleted": 6,
                            },
                        ],
                    },
                )
            )
            panel = str(app.query_one(CheckpointPanel).body.plain)
            assert "DIRTY" in panel
            assert "2 files · +34 -11" in panel
            assert "src/parser.py" in panel
            assert "+24 -5" in panel

            app._consume_event(
                event(
                    "checkpoint_created",
                    {"commit": "a31fc42deadbeef", "message": "gcae: fix parser", "kind": "step"},
                )
            )
            app._consume_event(
                event("candidate_state", {"dirty": False, "added": 0, "deleted": 0, "files": []})
            )
            panel = str(app.query_one(CheckpointPanel).body.plain)
            assert "CLEAN" in panel
            assert runtime.state.accepted_commit[:7] in panel
            assert "gcae: fix parser" in panel

    asyncio.run(scenario())


def test_rollback_is_prominent_then_fades(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "rollback_completed",
                    {
                        "from_commit": "d91f220aaaa",
                        "to_commit": "a31fc42bbbb",
                        "reason": "public API changed unnecessarily",
                        "discarded": ["src/parser.py", "tests/test_parser.py"],
                    },
                )
            )
            panel = str(app.query_one(CheckpointPanel).body.plain)
            assert "ROLLBACK" in panel
            assert "2 files discarded" in panel
            assert "a31fc42" in panel
            assert "public API changed unnecessarily" in panel
            status = str(app.query_one(StatusBar).body.plain)
            assert "ROLLBACK" in status
            timeline = str(app.query_one(TimelinePanel).body.plain)
            assert "rollback" in timeline
            assert app.ui.rollbacks >= 0

            app.ui.rollback_at = datetime(2000, 1, 1, tzinfo=UTC)
            app._refresh_panels({"checkpoint", "status"})
            assert "ROLLBACK" not in str(app.query_one(CheckpointPanel).body.plain)

    asyncio.run(scenario())


def test_context_and_memory_metrics(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "context_built",
                    {"characters": 39200, "estimated_tokens": 9800, "pinned": 4, "omitted": 2},
                )
            )
            app._consume_event(
                event("memory_updated", {"counts": {"fact": 18, "decision": 6, "failure": 2}})
            )
            metrics = str(app.query_one(MetricsPanel).body.plain)
            assert "9.8k/8.2k" in metrics or "CTX" in metrics
            assert "18 facts" in metrics
            assert "6 decisions" in metrics
            assert "2 failed paths" in metrics

    asyncio.run(scenario())


def test_model_role_changes_update_the_status_bar(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(150, 40)) as pilot:
            await pilot.pause()
            app._consume_event(event("phase:plan", phase=RunPhase.PLAN))
            assert "· PLAN" in str(app.query_one(StatusBar).body.plain)
            app._consume_event(event("phase:evaluate", phase=RunPhase.EVALUATE))
            assert "· EVAL" in str(app.query_one(StatusBar).body.plain)
            app._consume_event(event("phase:verify", phase=RunPhase.VERIFY))
            assert "· VERIFY" in str(app.query_one(StatusBar).body.plain)
            app._consume_event(event("phase:execute", phase=RunPhase.EXECUTE))
            assert "· ACT" in str(app.query_one(StatusBar).body.plain)

    asyncio.run(scenario())


# ------------------------------------------------------------------ interaction


def test_control_shortcuts(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("p")
            assert app.control.paused
            await pilot.press("r")
            assert not app.control.paused
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmStopModal)
            await pilot.press("escape")
            await pilot.pause()
            assert not app.control.stopped
            await pilot.press("s")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            assert app.control.stopped

    asyncio.run(scenario())


def test_help_and_panel_focus_shortcuts(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            assert isinstance(app.screen, HelpModal)
            assert "GCAE keys" in str(app.screen.body.plain)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpModal)

            first = app.focused
            await pilot.press("j")
            await pilot.pause()
            assert app.focused is not first or app.focused.id is not None
            await pilot.press("tab")
            await pilot.pause()
            assert app.focused is not None

    asyncio.run(scenario())


def test_enter_opens_the_focused_panel_detail(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            app.query_one(PlanPanel).focus()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, PlanScreen)
            await pilot.press("escape")
            await pilot.pause()
            app.query_one(CheckpointPanel).focus()
            await pilot.press("enter")
            for _ in range(60):
                if isinstance(app.screen, DiffScreen):
                    break
                await pilot.pause(0.05)
            assert isinstance(app.screen, DiffScreen)
            await pilot.press("escape")

    asyncio.run(scenario())


class _LiveLoop:
    """Stand-in for a running agent worker, so the queue path is exercised deterministically."""

    is_running = True


def test_instruction_modal_queues_an_instruction_while_the_loop_runs(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app._agent = _LiveLoop()  # type: ignore[assignment]
            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, InstructionModal)
            for character in "preserve streaming":
                await pilot.press("space" if character == " " else character)
            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, InstructionModal)
            assert app.control.take_instructions() == ["preserve streaming"]
            texts = [row.text for row in app.ui.timeline]
            assert any("preserve streaming" in text for text in texts)

    asyncio.run(scenario())


def test_instruction_revives_a_run_that_stalled_waiting_for_the_user(tmp_path: Path) -> None:
    """A stalled loop must act on the instruction instead of queueing it into nothing."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    replan = {"action": "replan", "semantic_goal": "s", "reason_summary": "wrong assumption"}
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(
            [
                replan,
                replan,
                replan,
                # consumed after the user's instruction revives the run
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
        ),
        control=RuntimeControl(),
    )
    app = GcaeApp(runtime, request="do the work", criteria=["file exists: answer.txt"])

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            def stalled() -> bool:
                state = app.runtime.state
                return state is not None and state.status == "waiting_for_user"

            await wait_for(pilot, stalled)
            assert runtime.state is not None
            assert runtime.state.status == "waiting_for_user"
            assert runtime.state.pending_question
            await pilot.press("i")
            await pilot.pause()
            for character in "keep it small":
                await pilot.press("space" if character == " " else character)
            await pilot.press("enter")
            await wait_for(
                pilot, lambda: runtime.state is not None and runtime.state.status == "complete"
            )
            assert (source / "answer.txt").read_text() == "ok"

    asyncio.run(scenario())


def test_request_modal_starts_a_run_and_recovers_after_failure(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "T"], check=True)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(TRAJECTORY),
        control=RuntimeControl(),
        auto_bootstrap=False,
    )
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RequestModal)
            for character in "first":
                await pilot.press(character)
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.agent_done)
            assert app.last_error is not None
            assert "no commits" in app.last_error
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN FAILED" in banner
            assert "no commits" in banner

            (source / "README").write_text("base\n")
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)

            await pilot.press("i")
            await pilot.pause()
            assert isinstance(app.screen, RequestModal)
            for character in "second":
                await pilot.press(character)
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.agent_done)
            assert runtime.state is not None
            assert runtime.state.status == "complete"

    asyncio.run(scenario())


# ---------------------------------------------------------------- detail screens


def test_unborn_repository_is_bootstrapped_and_reported(tmp_path: Path) -> None:
    """`git init` plus a run must work without manual Git steps, and must say what it did."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "README").write_text("hello\n")
    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider(TRAJECTORY), control=RuntimeControl()
    )
    app = GcaeApp(runtime, request="create answer", criteria=["file exists: answer.txt"])

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            assert runtime.state is not None
            assert runtime.state.status == "complete"
            assert app.last_error is None
            texts = [row.text for row in app.ui.timeline]
            assert any("base commit created" in text for text in texts)
            assert any("1 files" in text for text in texts)
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN COMPLETE" in banner

    asyncio.run(scenario())


def test_request_text_is_preserved_verbatim(tmp_path: Path) -> None:
    """The typed task reaches the runtime and the dashboard unchanged."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider(TRAJECTORY), control=RuntimeControl()
    )
    app = GcaeApp(runtime)
    task = "create hello.md with lorem ipsum"

    async def scenario() -> None:
        async with app.run_test(size=(90, 30)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, RequestModal)
            for character in task:
                key = {" ": "space", ".": "full_stop"}.get(character, character)
                await pilot.press(key)
            await pilot.press("enter")
            await wait_for(pilot, lambda: app.agent_done)
            assert runtime.state is not None
            assert runtime.state.original_request == task
            assert runtime.state.objective == task
            objective = str(app.query_one(ObjectivePanel).body.plain)
            assert "create hello.md" in objective
            texts = [row.text for row in app.ui.timeline]
            assert any("task accepted" in text for text in texts)
            assert any("hello.md" in text for text in texts)

    asyncio.run(scenario())


def test_diff_screen_lists_files_and_switches(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert runtime.state is not None
    worktree = Path(runtime.state.worktree)
    (worktree / "README").write_text("base\nmodified\n")
    (worktree / "fresh.txt").write_text("fresh line\n")
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("d")
            await wait_for(pilot, lambda: isinstance(app.screen, DiffScreen))
            screen = app.screen
            assert isinstance(screen, DiffScreen)
            listing = screen.query_one("#diff-files")
            assert len(listing.children) == 2
            body = str(screen.body.plain)
            assert "modified" in body or "fresh line" in body
            listing.index = 1
            await pilot.pause()
            body = str(screen.body.plain)
            assert "fresh line" in body or "modified" in body
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, DiffScreen)

    asyncio.run(scenario())


def test_logs_screen_filters(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            app._consume_event(
                event("tool_result", {"tool": "run_command", "success": True, "duration_ms": 12.0})
            )
            app._consume_event(
                event(
                    "decision",
                    {"action": "execute_tool", "tool": {"name": "read_file", "arguments": {}}},
                )
            )
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(app.screen, LogsScreen)
            assert app.screen.viewer_title == "logs · all events"
            await pilot.press("f")
            await pilot.pause()
            title = app.screen.viewer_title
            assert "model" in title
            body = str(app.screen.body.plain)
            assert "[decision]" in body
            await pilot.press("escape")

    asyncio.run(scenario())


def test_memory_context_evaluation_plan_screens(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    assert runtime.state is not None
    runtime._remember("fact", "parser drops quotes at chunk boundaries")
    runtime._remember("failure", "boundary fix regressed streaming", immutable=True)
    runtime.last_context_text = "Objective: create answer\nPlan: step-1 [active] create\n"
    runtime.last_context_info = {
        "characters": 55,
        "estimated_tokens": 14,
        "pinned": 2,
        "omitted": 0,
    }
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.press("m")
            for _ in range(60):
                if isinstance(app.screen, MemoryScreen):
                    break
                await pilot.pause(0.05)
            assert isinstance(app.screen, MemoryScreen)
            body = str(app.screen.body.plain)
            assert "FACT" in body
            assert "parser drops quotes at chunk boundaries" in body
            assert "FAILURE" in body
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()
            assert isinstance(app.screen, ContextScreen)
            assert len(app.screen.sections) >= 2
            body = str(app.screen.body.plain)
            assert "objective" in body
            await pilot.press("escape")
            await pilot.pause()

            app._consume_event(
                event(
                    "evaluation",
                    {
                        "decision": "rollback",
                        "reason": "candidate modified the public API unnecessarily",
                        "next_goal": "restore the public signature",
                        "memories_to_promote": [
                            {"record": {"kind": "fact", "content": "API must stay stable"}}
                        ],
                    },
                )
            )
            app._consume_event(
                event(
                    "verification_completed",
                    {
                        "passed": False,
                        "criteria": [
                            {
                                "criterion": "command succeeds: pytest",
                                "passed": False,
                                "evidence": "2 failed",
                            }
                        ],
                    },
                )
            )
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, EvaluationScreen)
            body = str(app.screen.body.plain)
            assert "rollback" in body
            assert "candidate modified the public API unnecessarily" in body
            assert "restore the public signature" in body
            await pilot.press("escape")
            await pilot.pause()

            await pilot.press("t")
            await pilot.pause()
            assert isinstance(app.screen, PlanScreen)
            body = str(app.screen.body.plain)
            assert "step-1" in body
            assert "requested" in body
            await pilot.press("escape")

    asyncio.run(scenario())


# -------------------------------------------------------------------- responsive


@pytest.mark.parametrize("size", [(160, 45), (120, 35), (90, 30), (80, 24), (60, 18)])
def test_responsive_smoke(tmp_path: Path, size: tuple[int, int]) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "candidate_state",
                    {
                        "dirty": True,
                        "added": 5,
                        "deleted": 1,
                        "files": [{"code": " M", "path": "README", "added": 5, "deleted": 1}],
                    },
                )
            )
            app._consume_event(event("step_started", {"goal": "do work", "index": 1, "total": 2}))
            await pilot.pause()
            width, _ = size
            main = app.query_one("#main")
            assert main.has_class("stacked") == (width < 100)
            assert app.query_one(MetricsPanel).display == (width >= 90)
            assert str(app.query_one(ObjectivePanel).body.plain).strip() != ""
            assert str(app.query_one(ActivityPanel).body.plain).strip() != ""
            assert app.size.width == width

    asyncio.run(scenario())


def test_goal_text_is_not_truncated_to_nothing_on_narrow_terminals(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app._consume_event(
                event(
                    "step_started",
                    {
                        "goal": "identify why quoted records fail across chunk",
                        "index": 1,
                        "total": 1,
                    },
                    step_id="step-1",
                )
            )
            objective = str(app.query_one(ObjectivePanel).body.plain)
            assert "quoted records fail across chunk" in objective

    asyncio.run(scenario())


# --------------------------------------------------------------------- reducer


def test_ui_state_reducer_is_targeted_and_bounded() -> None:
    ui = UiState()
    assert ui.apply(event("run_started", {"objective": "fix parser"})) >= {"status", "timeline"}
    snapshot = {"dirty": True, "files": [], "added": 0, "deleted": 0}
    changed = ui.apply(event("candidate_state", snapshot))
    assert "checkpoint" in changed
    assert "validation" not in changed
    assert ui.candidate is not None and ui.candidate["dirty"] is True
    ui.apply(event("validation", {"passed": True, "command_results": []}))
    assert "validation" in ui.apply(event("evaluation", {"decision": "accept", "reason": "good"}))
    assert ui.evaluation is not None
    for index in range(1000):
        ui.apply(event("tool_result", {"tool": f"t{index}", "success": True}))
    assert len(ui.logs) <= 2000


def test_formatters() -> None:
    assert formatters.duration(0.4) == "0.4s"
    assert formatters.duration(45) == "45s"
    assert formatters.duration(763) == "12m43s"
    assert formatters.duration(3720) == "1h02m"
    assert formatters.elide("abcdefgh", 5) == "abcd…"
    assert formatters.short_id("a31fc42deadbeef") == "a31fc42"
    assert formatters.short_id(None) == "-"
    bar, label = formatters.context_usage({"estimated_tokens": 9800}, 32000, bar_width=10)
    assert bar == "███░░░░░░░"
    assert "9.8k/32.0k" in label and "30%" in label
    assert formatters.plan_marker("completed") == ("✓", "done")
    assert formatters.plan_marker("active") == ("●", "current")
    assert formatters.plan_marker("failed") == ("×", "error")
    assert formatters.file_label(" M") == "M"
    assert formatters.file_label("??") == "A"
    clean = {"dirty": False, "files": [], "added": 0, "deleted": 0}
    assert formatters.snapshot_summary(clean) == "CLEAN"
    assert formatters.memory_summary({}) == "empty"
    lines = formatters.tool_argument_lines(
        "read_file", {"path": "src/parser.py", "content": "x" * 200, "offset": 3}
    )
    assert lines[0] == "src/parser.py"
    assert "200 chars" in lines[1]
    assert lines[2] == "offset=3"
    assert all("{" not in line for line in lines)
    assert formatters.timeline_entry("tool_result", {"tool": "read_file"}, succeeded=True) is None
    failed = formatters.timeline_entry(
        "tool_result", {"tool": "read_file", "error": "boom"}, False
    )
    assert failed is not None and failed[0] == "×"
    assert formatters.timeline_entry("memory_updated", {"counts": {}}) is None
    assert formatters.timeline_entry("repetition_detected", {"tool": "read_file"})[0] == "!"
    sections = formatters.context_sections("Objective: x\nPlan: y\nCurrent diff:\n+a\n-b\n")
    assert [name for name, _, _, _ in sections] == ["objective", "plan", "diff"]
    assert sections[2][1] == len("Current diff:\n+a\n-b")
    assert formatters.status_style("failed: provider output") == "error"
    assert formatters.run_badge("complete", paused=False) == ("COMPLETE", "success")
    assert formatters.run_badge("running", paused=True) == ("PAUSED", "warning")
    assert formatters.role_for_phase("evaluate") == "EVAL"


def test_completed_run_is_merged_automatically(tmp_path: Path) -> None:
    """The user must see the work in their checkout when the run completes."""
    runtime = make_runtime(tmp_path)
    assert runtime.auto_merge is True
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            await wait_for(
                pilot, lambda: "merged into" in str(app.query_one(BannerPanel).body.plain)
            )
            assert runtime.state is not None and runtime.state.merge is not None
            assert (tmp_path / "source" / "answer.txt").read_text() == "ok"
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "gcae undo" in banner
            texts = [row.text for row in app.ui.timeline]
            assert any("merged into" in text for text in texts)

    asyncio.run(scenario())


def test_auto_merge_off_offers_a_merge_key(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.auto_merge = False
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            assert runtime.state is not None and runtime.state.merge is None
            assert not (tmp_path / "source" / "answer.txt").exists()
            footer = str(app.query_one(FooterBar).body.plain)
            assert "[M] Merge" in footer
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "not merged" in banner

            await pilot.press("M")
            await wait_for(pilot, lambda: runtime.state.merge is not None)
            assert (tmp_path / "source" / "answer.txt").read_text() == "ok"
            assert "[M] Merge" not in str(app.query_one(FooterBar).body.plain)

    asyncio.run(scenario())


def test_merge_with_nothing_to_merge_is_reported_quietly(tmp_path: Path) -> None:
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
    app = GcaeApp(runtime, request="do nothing", criteria=["file exists: README"])

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            await wait_for(
                pilot, lambda: any("nothing to merge" in row.text for row in app.ui.timeline)
            )
            assert runtime.state is not None and runtime.state.merge is None

    asyncio.run(scenario())


def test_failed_run_offers_to_rescue_accepted_work(tmp_path: Path) -> None:
    """A run that accepted work and then failed must still be recoverable from the dashboard."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
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
                {
                    "action": "execute_tool",
                    "semantic_goal": "more",
                    "reason_summary": "keep going",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "second.txt", "content": "more"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "more",
                    "reason_summary": "done",
                },
            ]
        ),
        control=RuntimeControl(),
        max_steps=3,
    )
    app = GcaeApp(runtime, request="create answer", criteria=["file exists: second.txt"])

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await wait_for(
                pilot, lambda: "merged into" in str(app.query_one(BannerPanel).body.plain)
            )
            assert runtime.state is not None
            assert runtime.state.status.startswith("failed")
            assert runtime.state.merge is not None
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "RUN FAILED" in banner
            assert "final verification did not pass" in banner
            assert "undo: gcae undo" in banner
            assert (source / "answer.txt").exists()
            assert runtime.state.worktree and not Path(runtime.state.worktree).exists()

    asyncio.run(scenario())


def test_failed_run_keeps_its_work_when_automatic_rescue_is_off(tmp_path: Path) -> None:
    """With merge_accepted_on_failure off the dashboard still offers M."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
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
                {
                    "action": "execute_tool",
                    "semantic_goal": "more",
                    "reason_summary": "keep going",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "second.txt", "content": "more"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "more",
                    "reason_summary": "done",
                },
            ]
        ),
        control=RuntimeControl(),
        max_steps=3,
        merge_accepted_on_failure=False,
    )
    app = GcaeApp(runtime, request="create answer", criteria=["file exists: second.txt"])

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            assert runtime.state is not None and runtime.state.merge is None
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "accepted step(s) are on branch" in banner
            assert "[M] Merge accepted work" in str(app.query_one(FooterBar).body.plain)
            await pilot.press("M")
            await wait_for(
                pilot, lambda: "merged into" in str(app.query_one(BannerPanel).body.plain)
            )
            assert (source / "answer.txt").exists()

    asyncio.run(scenario())


def test_completion_banner_shows_the_documents_and_where_they_are(tmp_path: Path) -> None:
    """'Show me the documents physically' must be answerable from the dashboard."""
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(140, 44)) as pilot:
            await wait_for(
                pilot, lambda: "merged into" in str(app.query_one(BannerPanel).body.plain)
            )
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "files" in banner
            assert "answer.txt" in banner
            # the physical folder is named, so the file can be opened directly
            assert str(tmp_path / "source") in banner

    asyncio.run(scenario())


def test_unmerged_banner_points_at_the_worktree_folder(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path)
    runtime.auto_merge = False
    app = GcaeApp(runtime)

    async def scenario() -> None:
        async with app.run_test(size=(140, 44)) as pilot:
            await wait_for(pilot, lambda: app.agent_done)
            await pilot.pause(0.4)
            banner = str(app.query_one(BannerPanel).body.plain)
            assert "answer.txt" in banner
            assert "worktree, not merged yet" in banner
            assert runtime.state is not None and str(runtime.state.worktree) in banner

    asyncio.run(scenario())


def test_dashboard_hands_a_merge_conflict_to_the_agent(tmp_path: Path) -> None:
    """Conflicts are the agent's job in the dashboard too, and the merge is retried."""
    runtime = make_runtime(tmp_path)
    app = GcaeApp(runtime, auto_run=False)
    calls: list[str] = []

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)):
            assert runtime.state is not None
            runtime.state.status = "complete"
            app.agent_done = True
            runtime.resolve_merge_conflicts = lambda: calls.append("resolved") or ["app.py"]  # type: ignore[method-assign]
            runtime.run = lambda: calls.append("ran") or runtime.state  # type: ignore[method-assign]
            runtime.merge_completed_run = lambda allow_unverified=False: calls.append("merged")  # type: ignore[method-assign,assignment]

            app._on_conflict(["app.py"])
            for _ in range(60):
                if "merged" in calls:
                    break
                await pilot_pause(app)
            assert calls == ["resolved", "ran", "merged"]
            texts = [row.text for row in app.ui.timeline]
            assert any("merge conflicts" in text for text in texts)

    async def pilot_pause(app: GcaeApp) -> None:
        import asyncio as _asyncio

        await _asyncio.sleep(0.05)

    asyncio.run(scenario())
