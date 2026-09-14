"""Dashboard tests: rendering, event updates, interaction, responsive layout."""

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("textual")

from gcae.models import Event, RunPhase
from gcae.planner import LLMPlanner
from gcae.providers import FakeProvider, StreamProgress
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
    EvaluationPanel,
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
            assert "awaiting a candidate to validate" in str(
                app.query_one(ValidationPanel).body.plain
            )

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
            # a model call in flight is one calm row: role, state, elapsed
            assert "model · waiting" in activity
            assert "s" in activity

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
            assert "× pytest tests/parser/" in panel
            assert "✓ ruff check ." in panel
            assert "✓ git diff --check" in panel
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
            assert "verification failed" in panel
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


def test_timeline_shows_the_self_diagnosis() -> None:
    """The dashboard is where the run's self-recovery has to be observable."""
    started = formatters.timeline_entry(
        "recovery_started", {"trigger": "stagnation: wrong assumption", "attempt": 1}
    )
    completed = formatters.timeline_entry(
        "recovery_completed",
        {
            "root_cause": "the patch had no valid input",
            "corrective_instruction": "write the file with create_file",
            "strategy": "replan",
        },
    )
    failed = formatters.timeline_entry("recovery_failed", {"error": "no diagnosis"})
    assert started is not None and "self-diagnosis #1" in started[1]
    assert completed is not None
    assert "the patch had no valid input" in completed[1]
    assert "write the file with create_file" in completed[1]
    assert failed is not None and "unavailable" in failed[1]


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
                # the self-diagnosis attempt consumes this and cannot parse it as a
                # diagnosis, so the run falls through to asking the user (see
                # _handle_stagnation's ladder: change hypothesis, escalate, diagnose, ask)
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
            # the merge runs in the worker after the loop finishes: wait for the record,
            # then for the file, instead of racing the worker thread
            await wait_for(
                pilot,
                lambda: runtime.state is not None and runtime.state.merge is not None,
            )
            await wait_for(pilot, lambda: (source / "answer.txt").exists())
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
            assert app.query_one(MetricsPanel).display == (width >= 80)
            # priority 1 at every size: objective/NOW, plan, active, checkpoint
            for panel in (ObjectivePanel, PlanPanel, ActivityPanel, CheckpointPanel):
                text = str(app.query_one(panel).body.plain).strip()
                assert text, f"{panel.__name__} rendered nothing at {size}"
            assert "NOW" in str(app.query_one(ObjectivePanel).body.plain)
            assert "CANDIDATE" in str(app.query_one(CheckpointPanel).body.plain)
            activity = str(app.query_one(ActivityPanel).body.plain)
            assert "do work" in activity
            assert "model · waiting" in activity or "RUNNING" in activity
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
    action, target = formatters.tool_presentation("read_file", {"path": "src/parser.py"})
    assert action == "Read src/parser.py" and target == "src/parser.py"
    action, target = formatters.tool_presentation("run_command", {"command": "pytest -q"})
    assert action == "pytest -q" and target == ""
    action, target = formatters.tool_presentation(
        "search_text", {"query": "population", "path": "src/"}
    )
    assert action == 'Search "population"' and target == "src/"
    action, _ = formatters.tool_presentation("create_file", {"path": "src/app.py"})
    assert action == "Create src/app.py"
    assert "{" not in action
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
    assert formatters.run_state("complete", "complete", False) == ("COMPLETE", "success")
    assert formatters.run_state("running", "execute", True) == ("PAUSED", "warning")
    assert formatters.run_state("running", "execute", False) == ("ACTING", "accent")
    assert formatters.run_state("running", "validate", False) == ("VALIDATING", "accent")
    assert formatters.run_state("running", "rollback", False) == ("ROLLING BACK", "accent")
    assert formatters.run_state("waiting_for_user", "plan", False) == ("WAITING", "warning")
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


def test_timeline_shows_failover_degradation_and_conflicts() -> None:
    """Failures the CLI explains must also be visible in the dashboard, not only in logs."""
    failover = formatters.timeline_entry(
        "model_failover", {"role": "controller", "model": "qwen/qwen3-coder"}
    )
    degraded = formatters.timeline_entry(
        "runtime_degraded", {"component": "memory store", "error": "database is locked"}
    )
    adopted = formatters.timeline_entry(
        "success_criteria_adopted", {"criteria": ["file exists: out.txt"]}
    )
    rollback_failed = formatters.timeline_entry("rollback_failed", {"reason": "worktree is busy"})
    repeated = formatters.timeline_entry(
        "repeated_failure", {"signature": "scope violation", "count": 3}
    )
    conflict = formatters.timeline_entry("conflict_detected", {"files": ["a.py", "b.py"]})
    resolved = formatters.timeline_entry("conflict_resolved", {})
    merged = formatters.timeline_entry(
        "merge_completed", {"target_branch": "main", "merge_commit": "a1b2c3d4e5f6"}
    )

    assert failover is not None and "controller" in failover[1] and "qwen" in failover[1]
    assert degraded is not None and "memory store" in degraded[1] and "locked" in degraded[1]
    assert adopted is not None and "criteria" in adopted[1]
    assert rollback_failed is not None and "rollback failed" in rollback_failed[1]
    assert repeated is not None and "3x" in repeated[1]
    assert conflict is not None and "2 file(s)" in conflict[1]
    assert resolved is not None and "re-verified" in resolved[1]
    assert merged is not None and "main" in merged[1] and "a1b2c3d" in merged[1]


def test_a_degraded_run_is_flagged_in_the_status_bar() -> None:
    ui = UiState()
    assert ui.degradations == []

    changed = ui.apply(
        Event(
            run_id="abc123",
            event_type="runtime_degraded",
            phase=None,
            timestamp=datetime.now(UTC),
            payload={"component": "state file", "error": "state file failed: OSError"},
        )
    )
    assert "status" in changed and "timeline" in changed
    assert ui.degradations and ui.degradations[0].startswith("state file")

    # a second failure of the same component does not duplicate the flag
    ui.apply(
        Event(
            run_id="abc123",
            event_type="runtime_degraded",
            phase=None,
            timestamp=datetime.now(UTC),
            payload={"component": "state file", "error": "state file failed: OSError"},
        )
    )
    assert len(ui.degradations) == 1


def _event(event_type: str, payload: dict[str, object], step_id: str | None = None) -> Event:
    return Event(
        run_id="test-run",
        event_type=event_type,
        step_id=step_id,
        timestamp=datetime.now(UTC),
        payload=payload,
    )


# =============================================================== redesign: event curation
# Model streaming is telemetry: it must never enter the semantic timeline and never put
# generated text on the main screen.  It belongs to the log screen.


def test_streaming_telemetry_never_reaches_the_semantic_timeline() -> None:
    ui = UiState()
    telemetry = [
        ("provider_started", {"role": "controller", "model": "qwen/qwen3-coder"}),
        ("provider_first_token", {"role": "controller", "elapsed_ms": 800.0}),
        (
            "provider_progress",
            {
                "role": "controller",
                "characters": 1300,
                "elapsed_ms": 3000.0,
                "preview": "def fib(",
            },
        ),
        ("provider_waiting", {"role": "planner", "elapsed_ms": 12000.0}),
        ("provider_finished", {"role": "controller", "elapsed_ms": 3400.0}),
    ]
    for event_type, payload in telemetry:
        ui.apply(event(event_type, payload))

    assert ui.timeline == [], "streaming telemetry must not appear in the timeline"
    assert len(ui.logs) == len(telemetry), "the log screen keeps every raw event"
    assert any("[model]" in line and "progress" in line for line in ui.logs)
    assert any("chars=1300" in line for line in ui.logs), "logs keep the raw counters"
    assert any("preview=def fib(" in line for line in ui.logs)


def test_semantic_events_do_appear_in_the_timeline() -> None:
    ui = UiState()
    ui.apply(
        event(
            "step_started",
            {"goal": "design the model", "index": 2, "total": 5},
            step_id="step-2",
        )
    )
    ui.apply(
        event(
            "validation",
            {"passed": False, "command_results": [{"success": False, "tool": "pytest"}]},
            step_id="step-2",
        )
    )
    ui.apply(
        event(
            "rollback_completed",
            {"reason": "global mutable state", "to_commit": "abc1234", "discarded": ["a.py"]},
            step_id="step-2",
        )
    )
    icons = [row.icon for row in ui.timeline]
    assert "●" in icons and "×" in icons and "↩" in icons
    assert any("rollback" in row.text for row in ui.timeline)


def test_a_dirty_candidate_is_announced_once_per_step() -> None:
    """candidate_state fires after every tool call; the timeline must not flood."""
    ui = UiState()
    ui.apply(
        event("step_started", {"goal": "write the model", "index": 1, "total": 2}, step_id="s1")
    )
    dirty = {
        "dirty": True,
        "files": [{"code": " M", "path": "src/model.py", "added": 10, "deleted": 2}],
        "added": 10,
        "deleted": 2,
    }
    for _ in range(5):
        ui.apply(event("candidate_state", dirty, step_id="s1"))
    changed = [row for row in ui.timeline if "candidate changed" in row.text]
    assert len(changed) == 1, "one announcement per step, not one per tool call"
    assert "+10 -2" in changed[0].text

    ui.apply(event("step_started", {"goal": "next step", "index": 2, "total": 2}, step_id="s2"))
    ui.apply(event("candidate_state", dirty, step_id="s2"))
    assert len([row for row in ui.timeline if "candidate changed" in row.text]) == 2


def _rendered(app: GcaeApp, panel_type: type, ui: UiState, **kwargs: object) -> str:
    """Render one panel inside a running app and return its text."""

    async def scenario() -> str:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            app.ui = ui
            panel = app.query_one(panel_type)
            panel.render_state(app.runtime.state, ui, **kwargs)  # type: ignore[attr-defined]
            return str(panel.body.plain)

    return asyncio.run(scenario())


def test_the_active_panel_shows_state_not_streaming(tmp_path: Path) -> None:
    """No character counts, no partial generation: the panel reports the operation."""
    ui = UiState()
    ui.apply(
        event(
            "decision",
            {
                "action": "execute_tool",
                "reason_summary": "create the model file",
                "expected_result": "state and one transition exist",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "src/model.py", "content": "x" * 400},
                },
            },
            step_id="step-1",
        )
    )
    ui.apply(
        event(
            "provider_progress",
            {"role": "controller", "characters": 982, "preview": "import random\nclass"},
        )
    )
    body = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False),
        ActivityPanel,
        ui,
        model="qwen/qwen3-coder",
    )
    assert "Create src/model.py" in body, "the action is human-readable, not a tool name"
    assert "src/model.py" in body, "the target is its own row"
    assert "create the model file" in body
    assert "RUNNING" in body
    assert "982" not in body, "character counts are telemetry"
    assert "import random" not in body, "partial generation is telemetry"


def test_the_active_panel_shows_one_calm_row_while_a_model_generates(tmp_path: Path) -> None:
    """A model call in flight is a state, not a transcript."""
    ui = UiState()
    ui.apply(
        event("step_started", {"goal": "design the model", "index": 1, "total": 3}, step_id="s1")
    )
    ui.apply(event("provider_started", {"role": "controller", "model": "qwen/qwen3-coder"}))
    ui.apply(
        event(
            "provider_progress",
            {
                "role": "controller",
                "characters": 2400,
                "reasoning_characters": 900,
                "preview": "class Population:\n    def __init__",
            },
        )
    )
    body = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False),
        ActivityPanel,
        ui,
        model="qwen/qwen3-coder",
    )
    assert "controller · generating" in body
    assert "qwen/qwen3-coder" in body
    assert "2400" not in body and "900" not in body, "no character telemetry"
    assert "class Population" not in body, "no generated text on the main screen"


def test_the_checkpoint_panel_separates_trusted_from_candidate(tmp_path: Path) -> None:
    class _State:
        accepted_commit = "1f383c6deaf"

    ui = UiState()
    ui.apply(
        event(
            "candidate_state",
            {
                "dirty": True,
                "files": [
                    {"code": " M", "path": "src/simulation.py", "added": 24, "deleted": 5},
                    {"code": "??", "path": "tests/test_simulation.py", "added": 3, "deleted": 0},
                ],
                "added": 27,
                "deleted": 5,
            },
        )
    )
    app = GcaeApp(make_runtime(tmp_path, start=False), auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(120, 35)) as pilot:
            await pilot.pause()
            panel = app.query_one(CheckpointPanel)
            panel.render_state(_State(), ui, subject="Base commit")  # type: ignore[arg-type]
            body = panel.body.plain
            assert "TRUSTED" in body and "1f383c6" in body
            assert "CANDIDATE" in body and "DIRTY" in body and "2 files" in body
            assert "src/simulation.py" in body and "+24 -5" in body

            ui.candidate = {"dirty": False, "files": [], "added": 0, "deleted": 0}
            panel.render_state(_State(), ui, subject="Base commit")  # type: ignore[arg-type]
            assert "CLEAN · no speculative changes" in panel.body.plain

    asyncio.run(scenario())


def test_the_validation_panel_reports_structured_checks(tmp_path: Path) -> None:
    ui = UiState()
    ui.apply(
        event(
            "validation",
            {
                "passed": False,
                "commands": ["pytest -q tests/test_population.py"],
                "command_results": [
                    {
                        "tool": "run_command",
                        "success": False,
                        "exit_code": 1,
                        "error": "Expected: 120\nActual: 0",
                    }
                ],
                "diff_check_passed": True,
            },
        )
    )
    body = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False), ValidationPanel, ui
    )
    assert "× pytest -q tests/test_population.py" in body
    assert "Expected: 120" in body, "the failing check explains itself"
    assert "✓ git diff --check" in body
    assert "decision" not in body, "the evaluator has its own panel"


def test_the_evaluation_panel_shows_the_decision_and_reason(tmp_path: Path) -> None:
    ui = UiState()
    ui.apply(
        event(
            "evaluation",
            {
                "decision": "rollback",
                "reason": "candidate introduced unnecessary global mutable state",
            },
        )
    )
    body = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False), EvaluationPanel, ui
    )
    assert "ROLLBACK" in body
    assert "candidate introduced unnecessary global mutable state" in body

    empty = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False), EvaluationPanel, UiState()
    )
    assert "waiting for the first evaluation" in empty


def test_each_run_state_renders_its_headline(tmp_path: Path) -> None:
    """The state matrix the redesign promises: every condition has a visible headline."""
    from gcae.tui.widgets import EvaluationPanel

    cases: list[tuple[str, list[tuple[str, dict[str, object]]], type, str]] = [
        (
            "candidate dirty",
            [
                (
                    "candidate_state",
                    {
                        "dirty": True,
                        "files": [{"code": " M", "path": "src/sim.py", "added": 8, "deleted": 1}],
                        "added": 8,
                        "deleted": 1,
                    },
                )
            ],
            CheckpointPanel,
            "DIRTY",
        ),
        (
            "validation failed",
            [
                (
                    "validation",
                    {
                        "passed": False,
                        "commands": ["pytest -q"],
                        "command_results": [
                            {"success": False, "exit_code": 1, "tool": "run_command"}
                        ],
                        "diff_check_passed": True,
                    },
                )
            ],
            ValidationPanel,
            "× pytest -q",
        ),
        (
            "rollback",
            [
                # a real run emits the decision first, then the rollback that acted on it
                (
                    "evaluation",
                    {
                        "decision": "rollback",
                        "reason": "global mutable state is not acceptable",
                    },
                ),
                (
                    "rollback_completed",
                    {
                        "reason": "global mutable state is not acceptable",
                        "to_commit": "abc1234",
                        "discarded": ["a.py"],
                    },
                ),
            ],
            EvaluationPanel,
            "global mutable state is not acceptable",
        ),
        (
            "accepted",
            [("evaluation", {"decision": "accept", "reason": "the goal was met"})],
            EvaluationPanel,
            "ACCEPTED",
        ),
        (
            "replan",
            [("evaluation", {"decision": "replan", "reason": "the assumption was wrong"})],
            EvaluationPanel,
            "REPLAN",
        ),
        (
            "finish candidate",
            [("evaluation", {"decision": "finish_candidate", "reason": "all steps done"})],
            EvaluationPanel,
            "FINISH CANDIDATE",
        ),
    ]
    app = GcaeApp(make_runtime(tmp_path, start=False), auto_run=False)

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            await pilot.pause()
            for label, events, panel_type, expected in cases:
                ui = UiState()
                for event_type, payload in events:
                    ui.apply(event(event_type, payload))
                app.ui = ui
                panel = app.query_one(panel_type)
                kwargs = {"subject": "Base commit"} if panel_type is CheckpointPanel else {}
                panel.render_state(app.runtime.state, ui, **kwargs)  # type: ignore[attr-defined]
                body = str(panel.body.plain)
                assert expected in body, f"{label}: {expected!r} not in {body!r}"

    asyncio.run(scenario())


def test_a_model_call_never_renders_generated_text() -> None:
    """Belt and braces for the central rule of the redesign."""
    from gcae.tui import formatters

    payload = {
        "role": "planner",
        "model": "qwen/qwen3-coder",
        "characters": 4096,
        "reasoning_characters": 12000,
        "elapsed_ms": 9000,
        "preview": '{"steps": [{"id": "step-1", "goal": "leaked"}]}',
    }
    assert formatters.timeline_entry("provider_progress", payload) is None
    assert formatters.timeline_entry("provider_first_token", payload) is None
    assert formatters.timeline_entry("provider_started", payload) is None
    log = formatters.log_line("provider_progress", "plan", payload)
    assert "step-1" not in log or "preview=" in log, "the log may keep the preview, labelled"
    assert "preview=" in log


def test_a_finished_tool_keeps_its_human_label_and_reports_the_outcome(tmp_path: Path) -> None:
    """`Read src/parser.py` must not revert to `read_file`, and the result is one line."""
    ui = UiState()
    ui.apply(
        event(
            "decision",
            {
                "action": "execute_tool",
                "reason_summary": "inspect the parser",
                "expected_result": "chunk handling located",
                "tool": {"name": "read_file", "arguments": {"path": "src/parser.py"}},
            },
            step_id="s1",
        )
    )
    ui.apply(
        event(
            "tool_result",
            {
                "tool": "read_file",
                "success": True,
                "duration_ms": 320.0,
                "output": "def feed(chunk):\n    return chunk\n",
            },
            step_id="s1",
        )
    )
    body = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False), ActivityPanel, ui
    )
    assert "Read src/parser.py" in body, body
    assert "read_file" not in body, "the raw tool name must not come back"
    assert "DONE" in body and "def feed(chunk)" in body, "one line of the real outcome"

    ui.apply(
        event(
            "tool_result",
            {"tool": "run_command", "success": False, "exit_code": 1, "error": "3 failed"},
            step_id="s1",
        )
    )
    failure = _rendered(
        GcaeApp(make_runtime(tmp_path, start=False), auto_run=False), ActivityPanel, ui
    )
    assert "FAILED" in failure and "exit 1" in failure and "3 failed" in failure


# =============================================================== layout stability
# The redesign's promise is a screen that does not move while the agent works.  Geometry is
# checked on the *rendered* app, not on the presentation state: a panel that grows by one row
# would push every panel below it and is exactly what this test exists to catch.


class _TrickleProvider:
    """Streams progress, then replays a trajectory — so the layout can be sampled across the
    shape changes a real call goes through (model waiting → generating → tool running → done)."""

    on_progress: Any = None
    model = "trickle-model"

    def __init__(
        self, outputs: list[dict[str, Any]], updates: int = 6, delay: float = 0.06
    ) -> None:
        self._outputs = iter(outputs)
        self.updates = updates
        self.delay = delay

    def complete(self, prompt: str, schema: Any) -> Any:
        del prompt
        for index in range(self.updates):
            if self.on_progress is not None:
                self.on_progress(
                    StreamProgress(
                        characters=(index + 1) * 240,
                        reasoning_characters=(index + 1) * 90,
                        elapsed_ms=index * 50,
                        preview=f"partial generation line {index} that must not be rendered",
                    )
                )
            time.sleep(self.delay)
        return schema.model_validate(next(self._outputs))


def test_panel_boxes_never_move_while_a_model_streams(tmp_path: Path) -> None:
    """The rendered layout is identical while the agent works, including across the shape
    changes of a call (waiting → generating → tool → done).

    The test also proves it sampled those transitions; a window that only spanned a steady
    state would not exercise the guard.  The completion banner is a deliberate end-state
    change: it may resize the flexible (`1fr`) boxes, but no panel may move.
    """
    from gcae.tui.state import PANELS

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = _TrickleProvider(
        [
            {
                # a real command that takes ~0.8s: the tool phase must be long enough to
                # sample, otherwise the ACTIVE panel's tool shape is never observed
                "action": "execute_tool",
                "semantic_goal": "write the answer",
                "reason_summary": "create the file",
                "expected_result": "answer.txt exists",
                "tool": {
                    "name": "run_command",
                    "arguments": {
                        "command": (
                            "python -c \"import time; time.sleep(0.8); "
                            "open('answer.txt','w').write('ok')\""
                        )
                    },
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "write the answer",
                "reason_summary": "done",
            },
            {
                "action": "finish_candidate",
                "semantic_goal": "finish",
                "reason_summary": "nothing left",
            },
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=LLMPlanner(FakeProvider([PLAN])),
        control=RuntimeControl(),
    )
    app = GcaeApp(
        runtime, request="create answer.txt", criteria=["file exists: answer.txt"], auto_run=False
    )
    #: boxes that absorb the slack; the completion banner may resize them
    FLEXIBLE = {"plan", "evaluation"}
    visible = [name for name in PANELS if name != "banner"]

    async def scenario() -> None:
        async with app.run_test(size=(160, 45)):
            # _launch_agent spawns the worker; _run_agent *is* the worker body and would block
            # the event loop, which is exactly what stops a test from seeing a live layout
            app._launch_agent()
            # Raw sleeps, not pilot.pause(): pause() waits for the screen to settle, which only
            # happens once the run is over — every sample would be post-mortem.
            for _ in range(200):
                if app.ui.stream is not None and not app.agent_done:
                    break
                await asyncio.sleep(0.02)
            assert app.ui.stream is not None and not app.agent_done, (
                "never observed a live streaming call"
            )

            samples: list[tuple[bool, dict[str, tuple[int, int, int, int]]]] = []
            activities: list[str] = []
            leaks: list[str] = []
            for _ in range(140):
                await asyncio.sleep(0.03)
                frame = {}
                for name in visible:
                    region = app.query_one(f"#{name}").region
                    frame[name] = (region.x, region.y, region.width, region.height)
                samples.append((app.agent_done, frame))
                activity = app.query_one(ActivityPanel).body.plain
                activities.append(activity)
                timeline = app.query_one(TimelinePanel).body.plain
                leaks.extend(
                    marker
                    for marker in ("chars", "reasoning", "partial generation")
                    if marker in timeline or marker in activity
                )
                if app.agent_done and len(samples) > 20:
                    break

            live = [frame for done, frame in samples if not done]
            assert len(live) >= 5, f"only {len(live)} live samples: {len(samples)} total"

            # the samples must cover the transitions, otherwise this guard proves nothing
            assert any("model" in text and "generating" in text for text in activities), activities
            assert any("time.sleep(0.8)" in text and "RUNNING" in text for text in activities), (
                activities[:6]
            )
            assert any("finished" in text for text in activities), activities[-3:]

            # 1. no box changes at all while the run is live
            first = live[0]
            for index, frame in enumerate(live[1:], start=2):
                moved = {
                    name: (first[name], frame[name])
                    for name in visible
                    if first[name] != frame[name]
                }
                assert not moved, f"layout moved while running (live sample {index}): {moved}"

            # 2. nothing ever moves; only the flexible boxes may resize (completion banner)
            origin = samples[0][1]
            for index, (_done, frame) in enumerate(samples[1:], start=2):
                moved = {
                    name: (origin[name], frame[name])
                    for name in visible
                    if origin[name][:3] != frame[name][:3]
                }
                assert not moved, f"panel moved at sample {index}: {moved}"
                resized = {
                    name: (origin[name][3], frame[name][3])
                    for name in visible
                    if name not in FLEXIBLE and origin[name][3] != frame[name][3]
                }
                assert not resized, f"non-flexible panel resized at sample {index}: {resized}"

            assert not leaks, f"telemetry reached the main screen: {set(leaks)}"

    asyncio.run(scenario())
