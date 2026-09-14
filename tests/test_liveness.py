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
from pathlib import Path
from typing import Any

import pytest

from gcae.models import Decision, Event, PlanStep
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
