"""Liveness: a slow or hung model must stay visible and must not wedge the run.

Covers the three guarantees of this layer:
1. every provider call is bracketed by events, and a silent one emits heartbeats;
2. streaming progress becomes rate-limited events, so the dashboard (and the log) move
   while tokens arrive;
3. a stalled stream is a detected failure handed to the recovery ladder, not an
   indefinite wait, and the dashboard renders the live stream and long steps legibly.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import pytest

from gcae.models import (
    AgentState,
    Decision,
    Evaluation,
    Event,
    InitialPlan,
    PlanStep,
    SemanticStep,
    ToolCall,
)
from gcae.providers import FakeProvider, ProviderOutputError, StreamProgress
from gcae.runtime import Runtime, RuntimeControl
from gcae.tui.state import ActionView, UiState


def init_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class SlowProvider:
    """Completes after a delay, reporting nothing: the heartbeat must cover the gap."""

    on_progress: Any = None
    model = "slow-model"

    def __init__(self, delay: float) -> None:
        self.delay = delay

    def complete(self, prompt: str, schema: Any) -> Any:
        del prompt
        import time

        time.sleep(self.delay)
        return schema.model_validate(
            {"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "slow"}
        )


class StreamingStub:
    """Emits progress updates without streaming: tests the event plumbing exactly."""

    on_progress: Any = None
    model = "streaming-model"

    def __init__(self, updates: int) -> None:
        self.updates = updates

    def complete(self, prompt: str, schema: Any) -> Any:
        del prompt
        for index in range(self.updates):
            if self.on_progress is not None:
                self.on_progress(
                    StreamProgress(
                        characters=(index + 1) * 100,
                        reasoning_characters=(index + 1) * 10,
                        elapsed_ms=index,
                        preview=f"delta {index}",
                    )
                )
        return schema.model_validate(
            {"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "streamed"}
        )


def collect(runtime: Runtime) -> list[Event]:
    events: list[Event] = []
    runtime.subscribe(events.append)
    return events


def test_a_silent_provider_call_emits_heartbeats(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    from gcae import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROVIDER_HEARTBEAT_SECONDS", 0.05)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=SlowProvider(0.3), control=RuntimeControl()
    )
    events = collect(runtime)
    runtime.start("do the work", success_criteria=["file exists: README"])
    runtime.run()

    kinds = [event.event_type for event in events]
    assert "provider_started" in kinds
    assert "provider_waiting" in kinds, "a silent call must keep the run visibly alive"
    assert "provider_finished" in kinds
    heartbeats = [event for event in events if event.event_type == "provider_waiting"]
    assert heartbeats[0].payload["role"] == "controller"
    assert heartbeats[-1].payload["elapsed_ms"] >= 100
    finished = next(event for event in events if event.event_type == "provider_finished")
    assert finished.payload["elapsed_ms"] >= 250
    assert finished.payload["model"] == "slow-model"


def test_streaming_progress_becomes_rate_limited_events(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=StreamingStub(40), control=RuntimeControl()
    )
    events = collect(runtime)
    runtime.start("do the work", success_criteria=["file exists: README"])
    runtime.run()

    progress = [event for event in events if event.event_type == "provider_progress"]
    first = [event for event in events if event.event_type == "provider_first_token"]
    assert len(first) == 1, "the first token is announced exactly once per call"
    assert first[0].payload["elapsed_ms"] == 0
    assert 1 <= len(progress) < 40, "40 instant updates must not flood the log"
    assert progress[-1].payload["characters"] > 0
    assert progress[-1].payload["preview"].startswith("delta")
    assert all("role" in event.payload for event in progress)


def test_progress_listener_is_detached_after_the_call(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = StreamingStub(2)
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    runtime.start("do the work", success_criteria=["file exists: README"])
    runtime.run()
    assert provider.on_progress is None, "the listener must not outlive the call"
    assert runtime.last_context_text  # the run really executed a step


def test_a_stalled_stream_is_a_detected_failure_not_an_endless_wait() -> None:
    """The provider's stall error is what the recovery ladder consumes."""
    import httpx

    from gcae.http_provider import OpenAICompatibleProvider

    class Stalling(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            return self

        def __next__(self) -> bytes:
            raise httpx.ReadTimeout("no data")

        def close(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stalling()
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, stall_timeout=0.05
    )
    with pytest.raises(ProviderOutputError, match="stalled"):
        provider.complete("prompt", Decision)


def test_a_silent_buffered_request_is_reported_as_a_stall() -> None:
    """When streaming is off, silence must still fail fast and honestly."""
    import httpx

    from gcae.http_provider import OpenAICompatibleProvider

    class Silent(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            return self

        def __next__(self) -> bytes:
            raise httpx.ReadTimeout("no data")

        def close(self) -> None:
            return None

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, headers={"content-type": "application/json"}, stream=Silent())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, stream=False, stall_timeout=0.05
    )
    with pytest.raises(ProviderOutputError, match="stalled"):
        provider.complete("prompt", Decision)


def test_state_is_persisted_before_the_planner_runs(tmp_path: Path) -> None:
    """A run must be discoverable and resumable while a slow planner is still thinking."""
    import threading

    from gcae.models import InitialPlan, PlanStep

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)

    class SlowPlanner:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()

        def plan(self, state: Any) -> InitialPlan:
            self.started.set()
            assert self.release.wait(10)
            return InitialPlan(
                objective=state.objective,
                steps=[PlanStep(id="step-1", goal="do it")],
            )

        def replan(self, state: Any, reason: str) -> list[PlanStep]:
            del state, reason
            return []

    planner = SlowPlanner()
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider([]),
        planner=planner,
        control=RuntimeControl(),
    )
    thread = threading.Thread(
        target=lambda: runtime.start("do the work", success_criteria=["file exists: README"]),
        daemon=True,
    )
    thread.start()
    assert planner.started.wait(10), "the planner call must start"
    states = list((tmp_path / "runtime" / "runs").glob("*/state.json"))
    assert states, "state.json must exist while the planner is still running"
    planner.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_recovery_from_a_checkpoint_uses_legal_phase_transitions(tmp_path: Path) -> None:
    """Reproduces `unexpected error: ValueError: invalid transition checkpoint -> plan`.

    The budget is checked right after a step is accepted, so recovery can start while the
    phase is CHECKPOINT; the run used to die inside its own recovery handler.
    """
    from gcae.models import PlanStep
    from gcae.planner import Planner

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "fib.py", "content": "print('fib')\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
            {
                "root_cause": "the plan has an open step",
                "corrective_instruction": "finish the remaining step",
                "strategy": "replan",
            },
            {
                "action": "execute_tool",
                "semantic_goal": "finish",
                "reason_summary": "finish it",
                "tool": {
                    "name": "write_file",
                    "arguments": {"path": "other.py", "content": "print('other')\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "finish",
                "reason_summary": "done",
            },
        ]
    )

    class TwoSteps(Planner):
        def plan(self, state: AgentState) -> InitialPlan:
            return InitialPlan(
                objective=state.objective,
                success_criteria=list(state.success_criteria),
                steps=[
                    PlanStep(id="step-1", goal="create fib.py", intended_scope=["fib.py"]),
                    PlanStep(id="step-2", goal="add other.py", intended_scope=["other.py"]),
                ],
            )

    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=TwoSteps(),
        control=RuntimeControl(),
        max_steps=2,          # exhausted exactly when step-1 has just been accepted
    )
    events = collect(runtime)
    runtime.start("create the files", success_criteria=["file exists: fib.py"])
    state = runtime.run()

    failures = [str(e.payload.get("reason")) for e in events if e.event_type == "run_failed"]
    assert not [reason for reason in failures if "unexpected error" in reason], failures
    assert "invalid transition" not in state.status
    assert state.status == "complete", state.status
    # the diagnosis must be visible: a recovery call is a model call like any other
    started = [e for e in events if e.event_type == "provider_started"]
    assert any(e.payload.get("role") == "recovery" for e in started), (
        "the recovery advisor call must be instrumented"
    )


def test_a_finished_plan_is_verified_at_the_budget_limit(tmp_path: Path) -> None:
    """A plan that completes exactly at the budget must verify, not self-diagnose."""
    from gcae.models import PlanStep
    from gcae.planner import Planner

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "fib.py", "content": "print('fib')\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )

    class SingleStep(Planner):
        def plan(self, state: AgentState) -> InitialPlan:
            return InitialPlan(
                objective=state.objective,
                success_criteria=list(state.success_criteria),
                steps=[PlanStep(id="step-1", goal="create fib.py", intended_scope=["fib.py"])],
            )

    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=SingleStep(),
        control=RuntimeControl(),
        max_steps=2,          # the second iteration finishes the plan
        recovery_attempts=2,
    )
    events = collect(runtime)
    runtime.start("create fib.py", success_criteria=["file exists: fib.py"])
    state = runtime.run()

    assert state.status == "complete", state.status
    assert state.accepted_steps == 1
    assert not [e for e in events if e.event_type == "recovery_started"], (
        "a finished plan must be verified, not diagnosed as an exhausted budget"
    )
    assert state.iteration <= 3


def test_the_context_diff_includes_untracked_files(tmp_path: Path) -> None:
    """The agent must see the file it just created, or it rewrites it forever.

    Observed live: a complete 30-line script was written six times because `git diff` skipped
    the untracked file, so the agent concluded its own file was missing or incomplete.
    """
    import subprocess

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider([]), control=RuntimeControl()
    )
    runtime.start("do the work", success_criteria=["file exists: new.py"])
    assert runtime.state is not None and runtime.repo is not None
    worktree = Path(runtime.state.worktree)
    (worktree / "new.py").write_text("def main():\n    return 0\n")

    diff = runtime.repo.diff()
    assert "new.py" in diff, "an untracked file must appear in the diff"
    assert "def main()" in diff, "its content must be visible, not just its name"
    # unchanged tracking semantics: validation still sees it as a new file
    entries = runtime.repo.status_entries()
    assert ("??", "new.py") in entries
    subprocess.run(["git", "-C", str(worktree), "add", "new.py"], check=True)
    tracked = runtime.repo.diff()
    assert "def main()" in tracked


def test_the_next_decision_context_contains_a_created_file(tmp_path: Path) -> None:
    """The guarantee behind the fix: what the agent wrote is in the next prompt."""
    from gcae.tools import ToolRegistry

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [{"action": "complete_semantic_step", "semantic_goal": "write", "reason_summary": "ok"}]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    runtime.start("create fib.py", success_criteria=["file exists: fib.py"])
    assert runtime.state is not None

    tools = ToolRegistry(runtime.state.worktree, 5)
    tools.execute(
        ToolCall(name="write_file", arguments={"path": "fib.py", "content": "print('fib')\n"})
    )
    plan = runtime.state.plan[0]
    runtime._decide(SemanticStep(id=plan.id, goal=plan.goal), plan, tools)
    assert "print('fib')" in runtime.last_context_text, "the created file must be visible"


# ------------------------------------- repeated failures must reach the ladder fast


class RewriteForever:
    """Writes the file the task asks for, then completes the step (the user's real job)."""

    on_progress: Any = None

    def __init__(self) -> None:
        self.calls = 0

    def complete(self, prompt: str, schema: Any) -> Any:
        self.calls += 1
        if self.calls == 1:
            return schema.model_validate(
                {
                    "action": "execute_tool",
                    "semantic_goal": "create fib.py",
                    "reason_summary": "write the script",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "fib.py", "content": "print('fib')\n"},
                    },
                }
            )
        return schema.model_validate(
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create fib.py",
                "reason_summary": "written",
            }
        )


def test_planner_prose_in_scope_does_not_roll_back_the_task(tmp_path: Path) -> None:
    """Reproduces the 18-minute loop: prose scope made every created file a "violation"."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=RewriteForever(), control=RuntimeControl()
    )
    runtime.start("create fib.py", success_criteria=["file exists: fib.py"])
    state = runtime.state
    assert state is not None
    # the planner's mistake: prose in the scope, and the task's own file not listed
    state.plan = state.plan[:1]
    state.plan[0].intended_scope = ["file creation", "script naming"]
    finished = runtime.run()

    assert finished.status == "complete", finished.status
    # the Runtime does not merge; the CLI/TUI does. The accepted file lives in the run worktree.
    assert (Path(finished.worktree) / "fib.py").exists(), "created work must survive validation"
    assert finished.accepted_steps >= 1


def test_an_out_of_scope_modification_is_flagged_but_not_fatal(tmp_path: Path) -> None:
    """Modifying an unscoped existing file is evidence for the evaluator, not a rollback."""
    import subprocess

    from gcae.tools import ToolRegistry
    from gcae.validation import DeterministicValidator

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / "parser.py").write_text("original\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "parser"], check=True)

    runtime = Runtime(
        source, tmp_path / "runtime", provider=FakeProvider([]), control=RuntimeControl()
    )
    runtime.start("do the work", success_criteria=["file exists: README"])
    assert runtime.state is not None and runtime.repo is not None
    worktree = Path(runtime.state.worktree)
    (worktree / "fib.py").write_text("print('new')\n")     # additive: never a violation
    (worktree / "parser.py").write_text("changed\n")        # existing file, out of scope

    validator = DeterministicValidator(
        runtime.repo, ToolRegistry(str(worktree), 5), [], 10
    )
    result = validator.validate(["README"])
    assert result.passed, "scope must not decide pass/fail"
    assert result.new_files == ["fib.py"]
    assert result.scope_violations == ["parser.py"], "existing out-of-scope edits stay visible"
    assert any("scope warning" in warning for warning in result.warnings)


class AlwaysWritesThenFails:
    """Writes a file and completes the step; the configured validation command always fails."""

    on_progress: Any = None

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: Any) -> Any:
        self.prompts.append(prompt)
        if "recovery advisor" in prompt:
            return schema.model_validate(
                {
                    "root_cause": "the same validation command keeps failing identically",
                    "corrective_instruction": "stop repeating it and ask the user",
                    "strategy": "ask_user",
                }
            )
        if not hasattr(self, "_toggle"):
            self._toggle = False
        self._toggle = not self._toggle
        if self._toggle:
            return schema.model_validate(
                {
                    "action": "execute_tool",
                    "semantic_goal": "write",
                    "reason_summary": "write the file",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "fib.py", "content": "print('fib')\n"},
                    },
                }
            )
        return schema.model_validate(
            {"action": "complete_semantic_step", "semantic_goal": "write", "reason_summary": "done"}
        )


def test_repeated_identical_failures_reach_the_ladder_before_the_budget(tmp_path: Path) -> None:
    """Identical rejections must trigger the ladder in ~3 attempts, not 20 iterations."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = AlwaysWritesThenFails()
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        control=RuntimeControl(),
        validator_commands=["false"],  # deterministic, identical failure every time
        max_steps=30,
    )
    events = collect(runtime)
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()

    assert state.status == "waiting_for_user", state.status
    assert state.iteration < 12, f"the ladder must fire early, took {state.iteration} iterations"
    repeated = [event for event in events if event.event_type == "repeated_failure"]
    assert repeated, "the repeated-failure guard must announce itself"
    assert repeated[0].payload["count"] == 3
    assert "command 1 failed" in repeated[0].payload["signature"]


def test_the_failure_streak_contract(tmp_path: Path) -> None:
    """The guard counts identical rejections, resets on a different one, and can be cleared."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(source, tmp_path / "runtime", provider=FakeProvider([]))

    assert runtime._record_failure("same") is False
    assert runtime._record_failure("same") is False
    assert runtime._record_failure("same") is True
    assert runtime._record_failure("different") is False, "a new failure starts over"
    assert runtime._record_failure("different") is False
    assert runtime._record_failure("different") is True
    runtime._clear_failure_streak()
    assert runtime._record_failure("different") is False


def test_a_named_validation_failure_reaches_the_next_context(tmp_path: Path) -> None:
    """The rejection reason must say what failed, so the next attempt is not blind."""
    from gcae.evaluator import DeterministicEvaluator
    from gcae.models import EvaluationInput, ToolResult, ValidationResult

    validation = ValidationResult(
        passed=False,
        command_results=[ToolResult(tool="run_command", success=False, error="exit 1")],
        diff_check_passed=False,
        changed_files=["fib.py"],
        scope_violations=["fib.py"],
    )
    evaluation = DeterministicEvaluator().evaluate(
        EvaluationInput(objective="o", semantic_goal="g", validation=validation)
    )
    assert evaluation.decision == "rollback"
    assert "command 1 failed" in evaluation.reason
    assert "git diff --check" in evaluation.reason
    assert "changed outside the intended scope: fib.py" in evaluation.reason
    assert evaluation.reason != "deterministic validation failed"


# ------------------------------------------------------------------ dashboard


def test_a_silent_model_call_marks_the_waiting_action() -> None:
    ui = UiState()
    ui.apply(
        Event(
            run_id="r",
            event_type="provider_started",
            payload={"role": "controller", "model": "m"},
        )
    )
    assert ui.provider_role == "controller"
    assert ui.action is not None and ui.action.state == "waiting"
    ui.apply(
        Event(
            run_id="r",
            event_type="provider_waiting",
            payload={"role": "controller", "characters": 0, "elapsed_ms": 12_000},
        )
    )
    assert ui.stream is not None and ui.stream["waiting"] is True


def test_the_activity_panel_renders_the_live_stream() -> None:
    from gcae.tui.widgets import ActivityPanel

    ui = UiState()
    ui.plan = [{"id": "step-1", "goal": "do the work", "status": "active"}]
    ui.current_step = ui.plan[0]
    ui.action = ActionView(label="controller", lines=["model"], state="running")
    ui.stream = {
        "characters": 1234,
        "reasoning_characters": 36_000,
        "elapsed_ms": 12_000,
        "preview": "carrying quote state across the boundary",
    }
    row = ActivityPanel._stream_row(ui, 120)
    assert row is not None
    text = row.plain
    assert "1.2k chars" in text and "36k reasoning" in text and "12s" in text
    assert "carrying quote state" in text

    ui.stream = {"characters": 0, "reasoning_characters": 0, "elapsed_ms": 12_000, "waiting": True}
    waiting = ActivityPanel._stream_row(ui, 120)
    assert waiting is not None and "no output yet" in waiting.plain

    ui.agent_done = True
    assert ActivityPanel._stream_row(ui, 120) is None, "no stream row after the run ends"


def test_timeline_reports_streaming_and_waiting() -> None:
    from gcae.tui import formatters

    streaming = formatters.timeline_entry(
        "provider_progress",
        {
            "role": "controller",
            "characters": 2048,
            "reasoning_characters": 36_000,
            "elapsed_ms": 5000,
        },
    )
    waiting = formatters.timeline_entry(
        "provider_waiting", {"role": "controller", "elapsed_ms": 20_000}
    )
    started = formatters.timeline_entry(
        "provider_started", {"role": "planner", "model": "m"}
    )
    assert streaming is not None
    assert "2.0k chars" in streaming[1]
    assert "36k chars reasoning" in streaming[1]
    assert waiting is not None and "no output yet" in waiting[1]
    assert started is not None and "planner request" in started[1]


def test_a_long_active_step_wraps_instead_of_being_cut_off() -> None:
    """The plan must stay readable when the current step is a long sentence."""
    from gcae.tui.widgets import PlanPanel

    ui = UiState()
    long_goal = (
        "inspect the parser architecture, the chunk feed path and every place quote state "
        "can be reset between chunks, including the buffering helper and its callers"
    )
    ui.plan = [
        {"id": "step-1", "goal": long_goal, "status": "active"},
        {"id": "step-2", "goal": "reproduce the failure", "status": "pending"},
    ]

    class _State:
        plan: list[PlanStep] = []
        accepted_steps = 0

    panel = PlanPanel()
    rendered: list[Any] = []
    panel.render_block = lambda meta, lines: rendered.append((meta, lines))  # type: ignore[assignment]
    panel.render_state(_State(), ui)  # type: ignore[arg-type]

    meta, lines = rendered[0]
    text = "\n".join(line.plain for line in lines)
    assert "inspect the parser architecture" in text
    flat = " ".join(text.split())
    assert "the buffering helper and its callers" in flat, "the active goal must be readable"
    assert text.count("\n") >= 2, "the long goal must occupy continuation rows"
    assert "reproduce the failure" in text, "neighbouring steps stay visible"
    assert meta.endswith("steps")


def test_dashboard_shows_streaming_progress_end_to_end(tmp_path: Path) -> None:
    """The real app renders a streamed call: the panel and the timeline both move."""
    pytest.importorskip("textual")
    from gcae.tui.app import GcaeApp
    from gcae.tui.widgets import TimelinePanel

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source, tmp_path / "runtime", provider=StreamingStub(6), control=RuntimeControl()
    )
    app = GcaeApp(
        runtime, request="do the work", criteria=["file exists: README"], auto_run=False
    )

    async def scenario() -> None:
        async with app.run_test(size=(130, 40)) as pilot:
            app._run_agent()
            for _ in range(200):
                if app.agent_done:
                    break
                await pilot.pause(0.05)
            await pilot.pause(0.3)
            timeline = app.query_one(TimelinePanel).body.plain
            # the timeline keeps the streamed progress visible after the run ends; the
            # ACTIVE row intentionally disappears once there is nothing running
            assert "streaming" in timeline or "first tokens" in timeline
            assert "controller" in timeline
            assert app.ui.stream is not None, "the last stream count stays readable"

    asyncio.run(scenario())


def test_unknown_provider_without_progress_support_is_fine(tmp_path: Path) -> None:
    """A provider that cannot stream must not need any change (FakeProvider has no state)."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [{"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "ok"}]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, control=RuntimeControl())
    events = collect(runtime)
    runtime.start("do the work", success_criteria=["file exists: README"])
    runtime.run()
    kinds = [event.event_type for event in events]
    assert "provider_started" in kinds and "provider_finished" in kinds
    assert "provider_progress" not in kinds
    assert provider.on_progress is None


# =============================================== universal recovery / resilience
# Every failure must be diagnosed before it is allowed to end a run, and the runtime's own
# bookkeeping (state, memory, events) must never be the reason a run dies.


def _one_step_runtime(tmp_path: Path, provider: Any, **kwargs: Any) -> Runtime:
    """A runtime whose single planned step writes a file, so a run has something to do."""
    from gcae.models import InitialPlan, PlanStep
    from gcae.planner import Planner

    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)

    class SingleStep(Planner):
        def plan(self, state: AgentState) -> InitialPlan:
            return InitialPlan(
                objective=state.objective,
                success_criteria=list(state.success_criteria),
                steps=[PlanStep(id="step-1", goal="create the file", intended_scope=["out.txt"])],
            )

    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=SingleStep(),
        control=RuntimeControl(),
        **kwargs,
    )
    return runtime


def test_a_broken_memory_store_does_not_kill_the_run(tmp_path: Path) -> None:
    """Knowledge is lost, the run is not: memory failures degrade and are reported."""
    import sqlite3

    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "out.txt", "content": "ok\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = _one_step_runtime(tmp_path, provider)
    events = collect(runtime)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    assert runtime.memory is not None

    def explode(*args: Any, **kwargs: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    runtime.memory.add = explode  # type: ignore[method-assign]
    state = runtime.run()

    assert state.status == "complete", state.status
    assert any("memory store failed" in item for item in state.degradations)
    degraded = [e for e in events if e.event_type == "runtime_degraded"]
    assert degraded and degraded[0].payload["component"] == "memory store"


def test_an_unwritable_state_file_stops_the_run_instead_of_lying(tmp_path: Path) -> None:
    """State is the one thing that does not degrade: a stale state.json would make resume lie."""
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "out.txt", "content": "ok\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = _one_step_runtime(tmp_path, provider)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    import gcae.persistence as persistence

    def explode(self: Any, state: Any) -> None:
        raise OSError("No space left on device")

    original = persistence.StateStore.save
    persistence.StateStore.save = explode  # type: ignore[method-assign]
    try:
        result = runtime.run()
    finally:
        persistence.StateStore.save = original  # type: ignore[method-assign]

    assert result.status.startswith("failed: run state could not be written"), result.status
    assert "stale checkpoint" in result.status
    assert any("state file failed" in item for item in result.degradations)
    assert result.accepted_commit is not None, "the trusted commit is still recorded in memory"


def test_a_state_failure_during_recovery_still_ends_the_run(tmp_path: Path) -> None:
    """Recovery needs the same write that just failed: the run stops instead of looping."""
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "create",
                "reason_summary": "create it",
                "tool": {
                    "name": "create_file",
                    "arguments": {"path": "out.txt", "content": "ok\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "create",
                "reason_summary": "done",
            },
        ]
    )
    runtime = _one_step_runtime(tmp_path, provider)
    events = collect(runtime)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    import gcae.persistence as persistence

    calls = {"n": 0}
    original = persistence.StateStore.save

    def explode(self: Any, state: Any) -> None:
        calls["n"] += 1
        raise OSError("disk went away")

    persistence.StateStore.save = explode  # type: ignore[method-assign]
    try:
        result = runtime.run()
    finally:
        persistence.StateStore.save = original  # type: ignore[method-assign]

    assert result.status.startswith("failed: run state could not be written")
    # one write fails and stops the loop; the second is the best-effort report of the failure
    assert calls["n"] <= 2, f"the run must stop on the first failed write, saw {calls['n']} writes"
    assert not [e for e in events if e.event_type == "recovery_started"], (
        "recovery cannot help: it persists through the same broken file"
    )


def test_a_failing_recovery_does_not_kill_the_run(tmp_path: Path) -> None:
    """The rescue path itself must be fail-soft, whatever breaks inside it."""

    class BrokenAdvisor:
        on_progress: Any = None

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str, schema: Any) -> Any:
            self.calls += 1
            if "recovery advisor" in prompt:
                raise RuntimeError("the advisor exploded")
            if self.calls <= 3:
                return schema.model_validate(
                    {"action": "replan", "semantic_goal": "s", "reason_summary": "stuck"}
                )
            return schema.model_validate(
                {"action": "finish_candidate", "semantic_goal": "s", "reason_summary": "done"}
            )

    runtime = Runtime(
        source_repo=_fresh_repo(tmp_path),
        runtime_dir=tmp_path / "runtime",
        provider=BrokenAdvisor(),
        control=RuntimeControl(),
        recovery_attempts=1,
    )
    events: list[Event] = []
    runtime.subscribe(events.append)
    runtime.start("do the work", success_criteria=["file exists: README"])
    state = runtime.run()

    # the ladder continues past the broken advisor: escalation is unavailable, so it asks
    assert state.status == "waiting_for_user", state.status
    assert any(e.event_type in {"runtime_degraded", "recovery_failed"} for e in events)
    assert state.pending_question


def _fresh_repo(tmp_path: Path) -> Path:
    source = tmp_path / f"repo-{len(list(tmp_path.glob('repo-*')))}"
    source.mkdir()
    init_repo(source)
    return source


def test_corrupt_state_reports_what_is_wrong(tmp_path: Path) -> None:
    from gcae.persistence import StateStore

    path = tmp_path / "runs" / "abc" / "state.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"run_id": "abc", "status": ')      # half-written file
    (path.parent / "events.jsonl").write_text("{}\n")

    with pytest.raises(RuntimeError) as caught:
        StateStore(path).load()
    message = str(caught.value)
    assert "corrupt or incomplete" in message
    assert "events.jsonl" in message, "the error must point at the surviving trace"


def test_advisor_criteria_rescue_a_failed_planner(tmp_path: Path) -> None:
    """A planner outage must still be diagnosable: the advisor may define done."""

    class PlannerFailsAdvisorHelps:
        on_progress: Any = None

        def complete(self, prompt: str, schema: Any) -> Any:
            if "recovery advisor" in prompt:
                return schema.model_validate(
                    {
                        "root_cause": "the planner call failed twice",
                        "corrective_instruction": "create out.txt so the run can be verified",
                        "strategy": "replan",
                        "success_criteria": ["file exists: out.txt"],
                    }
                )
            if "controller" not in prompt and "planner" in prompt:
                raise ProviderOutputError("planner is down")
            return schema.model_validate(
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "out.txt", "content": "ok\n"},
                    },
                }
            )

    class NoPlanner:
        def plan(self, state: Any) -> Any:
            raise ProviderOutputError("planner is down")

        def replan(self, state: Any, reason: str) -> list[Any]:
            del state, reason
            return []

    source = _fresh_repo(tmp_path)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=PlannerFailsAdvisorHelps(),
        planner=NoPlanner(),
        control=RuntimeControl(),
    )
    events: list[Event] = []
    runtime.subscribe(events.append)
    runtime.start("create out.txt")          # no criteria at all

    assert runtime.state is not None
    assert runtime.state.success_criteria == ["file exists: out.txt"], (
        "the advisor's criteria must be adopted when the run has none"
    )
    assert any(e.event_type == "success_criteria_adopted" for e in events)


# =============================================== repository lock (one run per repo)
# Two runs sharing a repository would interleave their merges into the source branch.


def test_a_second_run_on_the_same_repository_is_refused(tmp_path: Path) -> None:
    """The lock is cross-process: a run in another process blocks a new one, with a real message."""
    import subprocess
    import sys

    source = _fresh_repo(tmp_path)
    runtime_dir = tmp_path / "runtime"
    marker = tmp_path / "locked"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from gcae.runtime import RunLock\n"
                "lock = RunLock(Path(sys.argv[2]), Path(sys.argv[3]), 'run deadbeef')\n"
                "lock.acquire()\n"
                "Path(sys.argv[4]).write_text('locked')\n"
                "time.sleep(30)\n"
            ),
            str(Path(__file__).resolve().parents[1] / "src"),
            str(runtime_dir),
            str(source),
            str(marker),
        ]
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the helper process never took the lock"

        runtime = Runtime(
            source_repo=source,
            runtime_dir=runtime_dir,
            provider=FakeProvider([]),
            control=RuntimeControl(),
        )
        with pytest.raises(RuntimeError) as caught:
            runtime.start("do the work", success_criteria=["file exists: README"])
        message = str(caught.value)
        assert "already working on" in message
        assert "run deadbeef" in message, "the message names the run holding the lock"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_the_lock_is_released_when_the_run_object_is_done(tmp_path: Path) -> None:
    """A finished run must not block the next one on the same repository."""
    source = _fresh_repo(tmp_path)
    runtime_dir = tmp_path / "runtime"
    first = Runtime(
        source_repo=source,
        runtime_dir=runtime_dir,
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "out.txt", "content": "ok\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "create",
                    "reason_summary": "done",
                },
            ]
        ),
        control=RuntimeControl(),
    )
    first.start("create out.txt", success_criteria=["file exists: out.txt"])
    first.run()
    first.release_lock()

    second = Runtime(
        source_repo=source,
        runtime_dir=runtime_dir,
        provider=FakeProvider([]),
        control=RuntimeControl(),
    )
    second.start("another task", success_criteria=["file exists: README"])
    assert second.state is not None
    second.release_lock()


# =============================================== failover for every role


def test_a_broken_evaluator_fails_over_and_judges_again(tmp_path: Path) -> None:
    """Switching the evaluator is only worth it if the same work is judged again."""

    class StepProvider:
        on_progress: Any = None
        model = "primary"

        def __init__(self) -> None:
            self.actions = 0

        def complete(self, prompt: str, schema: Any) -> Any:
            self.actions += 1
            if self.actions == 1:
                return schema.model_validate(
                    {
                        "action": "execute_tool",
                        "semantic_goal": "create",
                        "reason_summary": "create it",
                        "tool": {
                            "name": "create_file",
                            "arguments": {"path": "out.txt", "content": "ok\n"},
                        },
                    }
                )
            return schema.model_validate(
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "create",
                    "reason_summary": "done",
                }
            )

    class BrokenProvider:
        model = "broken-evaluator"
        broken = True

        def complete(self, prompt: str, schema: Any) -> Any:
            del prompt, schema
            raise ProviderOutputError("evaluator model is offline")

    class SwitchableEvaluator:
        """Blind until the runtime swaps in the fallback provider."""

        on_progress: Any = None

        def __init__(self) -> None:
            self.provider: Any = BrokenProvider()

        def evaluate(self, payload: Any) -> Evaluation:
            del payload
            if getattr(self.provider, "broken", False):
                raise ProviderOutputError("evaluator model is offline")
            return Evaluation(
                decision="accept",
                reason="the file was created as asked",
                progress_score=0.8,
            )

    evaluator = SwitchableEvaluator()
    source = _fresh_repo(tmp_path)
    runtime = Runtime(
        source_repo=source,
        runtime_dir=tmp_path / "runtime",
        provider=StepProvider(),
        evaluator=evaluator,
        control=RuntimeControl(),
        role_providers={"escalation": FakeProvider([])},
    )
    events: list[Event] = []
    runtime.subscribe(events.append)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    state = runtime.run()

    failover = [e for e in events if e.event_type == "model_failover"]
    assert failover and failover[0].payload["role"] == "evaluator"
    assert state.status == "complete", state.status
    assert state.accepted_steps == 1, "the work was accepted by the fallback evaluator"


def test_a_repeated_failure_escalates_the_model_before_asking_the_user(tmp_path: Path) -> None:
    """Three identical rejections are a policy signal: try the stronger model, then ask."""
    from gcae.evaluator import DeterministicEvaluator
    from gcae.models import Evaluation

    class AlwaysRejects(DeterministicEvaluator):
        def evaluate(self, payload: Any) -> Evaluation:
            del payload
            return Evaluation(
                decision="rollback",
                reason="the step does not address the objective",
                progress_score=0.0,
            )

    class StepProvider:
        on_progress: Any = None
        model = "primary"

        def complete(self, prompt: str, schema: Any) -> Any:
            return schema.model_validate(
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "out.txt", "content": "ok\n"},
                    },
                }
            )

    fallback = FakeProvider([])
    fallback.model = "stronger"  # type: ignore[attr-defined]
    source = _fresh_repo(tmp_path)
    runtime = Runtime(
        source_repo=source,
        runtime_dir=tmp_path / "runtime",
        provider=StepProvider(),
        evaluator=AlwaysRejects(),
        control=RuntimeControl(),
        role_providers={"escalation": fallback},
        max_steps=12,
        recovery_attempts=0,
    )
    events: list[Event] = []
    runtime.subscribe(events.append)
    runtime.start("create out.txt", success_criteria=["file exists: out.txt"])
    runtime.run()

    failover = [e for e in events if e.event_type == "model_failover"]
    assert failover, "repeated identical failures must escalate before asking the user"
    assert failover[0].payload["role"] == "controller"
    assert failover[0].payload["model"] == "stronger"
    assert runtime.active_model() == "stronger", "later decisions use the stronger model"
