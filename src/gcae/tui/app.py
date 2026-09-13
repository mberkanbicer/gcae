from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Header, Input, RichLog, Static
from textual.worker import Worker

from ..git import GitError
from ..models import Event
from ..runtime import Runtime, RuntimeControl

HELP = (
    "q quit  ·  p pause  ·  r resume  ·  s stop  ·  d diff  ·  "
    "i instruction  ·  l logs  ·  ? help"
)


class InstructionScreen(ModalScreen[str | None]):
    """Modal input for a new user instruction."""

    BINDINGS = [("enter", "submit", "Submit")]

    def __init__(
        self,
        title: str = "New instruction (Enter submits, Esc cancels)",
        placeholder: str = "e.g. preserve streaming behavior",
    ) -> None:
        super().__init__()
        self.title_text = title
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="instruction-box"):
            yield Static(self.title_text)
            yield Input(placeholder=self.placeholder, id="instruction-input")

    def on_mount(self) -> None:
        self.query_one("#instruction-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_submit(self) -> None:
        """Fallback for when the input does not hold focus (small terminals, clicks)."""
        self.dismiss(self.query_one("#instruction-input", Input).value)

    def key_escape(self) -> None:
        self.dismiss(None)


class RequestScreen(InstructionScreen):
    """First-run modal that asks for the task; the planner derives criteria from it."""

    def __init__(self) -> None:
        super().__init__(
            title="Describe the task (Enter starts the run, Esc cancels)",
            placeholder="e.g. Add a --dry-run flag to the importer",
        )


class DiffScreen(ModalScreen[None]):
    """Scrollable candidate diff."""

    def __init__(self, diff: str) -> None:
        super().__init__()
        self.diff = diff

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="diff-scroll"):
            yield Static(self.diff or "(no candidate diff)", id="diff-body")
        yield Footer()

    def key_escape(self) -> None:
        self.dismiss(None)

    def key_q(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    def compose(self) -> ComposeResult:
        yield Static(
            "GCAE keys\n\n"
            "q  quit (stops a running agent safely)\n"
            "p  pause before the next model or tool action\n"
            "r  resume a paused agent\n"
            "s  stop the run and persist state\n"
            "d  inspect the current candidate diff\n"
            "i  inject a user instruction / override\n"
            "l  toggle the event log\n"
            "?  this help\n\n"
            "Esc closes this help.",
            id="help-body",
        )

    def key_escape(self) -> None:
        self.dismiss(None)


class GcaeApp(App[None]):
    """Live dashboard for a single GCAE run."""

    TITLE = "GCAE"
    SUB_TITLE = "Git-Checkpointed Adaptive Execution"
    BINDINGS = [
        ("q", "quit_app", "Quit"),
        ("p", "pause", "Pause"),
        ("r", "resume", "Resume"),
        ("s", "stop", "Stop"),
        ("d", "diff", "Diff"),
        ("i", "instruction", "Instruction"),
        ("l", "toggle_logs", "Logs"),
        ("question_mark", "help", "Help"),
    ]
    CSS = """
    #body { height: 1fr; }
    #panels { width: 3fr; min-width: 20; }
    #log { width: 2fr; min-width: 20; border: round $accent; }
    .panel { border: round $panel; padding: 0 1; margin-bottom: 0; }
    #help-bar { height: 1; color: $text-muted; }
    InstructionScreen, RequestScreen { align: center middle; }
    #instruction-box {
        width: 90%;
        max-width: 70;
        min-width: 20;
        height: auto;
        padding: 1 2;
        background: $surface;
    }
    #diff-scroll { height: 1fr; padding: 1 2; background: $surface; }
    #help-body { padding: 2 4; background: $surface; }
    """

    def __init__(
        self,
        runtime: Runtime,
        auto_run: bool = True,
        request: str | None = None,
        constraints: list[str] | None = None,
        criteria: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.control: RuntimeControl = runtime.control or RuntimeControl()
        runtime.control = self.control
        self.auto_run = auto_run
        self.request = request
        self.constraints = list(constraints or [])
        self.criteria = list(criteria or [])
        self.panel_state: dict[str, str] = {}
        self.log_visible = True
        self.last_error: str | None = None
        self._needs_start = request is not None and runtime.state is None
        self.log_lines: list[str] = []
        self.agent_done = False
        self.last_evaluation = "none"
        self._worker: Worker[None] | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with VerticalScroll(id="panels"):
                yield Static("", id="run", classes="panel")
                yield Static("", id="objective", classes="panel")
                yield Static("", id="plan", classes="panel")
                yield Static("", id="git", classes="panel")
                yield Static("", id="action", classes="panel")
                yield Static("", id="validation", classes="panel")
                yield Static("", id="memory", classes="panel")
                yield Static("", id="context", classes="panel")
                yield Static("", id="model", classes="panel")
            yield RichLog(id="log", highlight=False, markup=False, wrap=True)
        yield Static(HELP, id="help-bar")
        yield Footer()

    def on_mount(self) -> None:
        self.runtime.subscribe(self._on_event_threadsafe)
        self.set_interval(0.4, self._refresh)
        self._apply_layout()
        if self.runtime.state is None and self.request is None:
            self._prompt_for_request()
        elif self.auto_run:
            self._launch_worker()
        self._refresh()

    def on_resize(self) -> None:
        self._apply_layout()

    def _apply_layout(self) -> None:
        """Keep the log visible only when the terminal is wide enough."""
        try:
            log = self.query_one("#log", RichLog)
        except NoMatches:
            return
        log.display = self.log_visible and self.size.width >= 90

    def _prompt_for_request(self) -> None:
        self._log_line("waiting for the task description")
        self.push_screen(RequestScreen(), self._submit_request)

    def _submit_request(self, text: str | None) -> None:
        if not text or not text.strip():
            self._log_line("no task entered; press q to quit")
            return
        self.request = text.strip()
        self._needs_start = True
        self._log_line(f"task: {self.request}")
        if self.auto_run:
            self._launch_worker()

    def _launch_worker(self) -> None:
        self.agent_done = False
        self.last_error = None
        self._worker = self.run_worker(
            self._run_agent, thread=True, name="agent", exit_on_error=False
        )

    # ------------------------------------------------------------------ agent worker

    def _run_agent(self) -> None:
        try:
            if self._needs_start:
                assert self.request is not None
                self._needs_start = False
                self._call_ui(self._log_line, f"starting run for: {self.request}")
                self.runtime.start(
                    self.request,
                    hard_constraints=self.constraints,
                    success_criteria=self.criteria,
                )
            self.runtime.run()
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI log
            self._call_ui(self._on_agent_error, str(exc))
        finally:
            self._call_ui(self._on_finished)

    def _on_agent_error(self, message: str) -> None:
        self.last_error = message
        self._log_line(f"agent failed: {message}")
        self._set_panel(
            "run",
            f"failed:\n{message}\n\npress i to enter a new task, q to quit",
        )

    def _on_finished(self) -> None:
        self.agent_done = True
        state = self.runtime.state
        if state is not None:
            self._log_line(f"agent finished: {state.status}")
        self._refresh()

    # ------------------------------------------------------------------ events

    def _call_ui(self, callback: Callable[..., None], *args: Any) -> None:
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            callback(*args)

    def _on_event_threadsafe(self, event: Event) -> None:
        self._call_ui(self._on_event, event)

    def _on_event(self, event: Event) -> None:
        phase = event.phase.value if event.phase else "-"
        self._log_line(f"[{event.event_type}] {phase}")
        payload = event.payload
        if event.event_type == "decision":
            tool = payload.get("tool")
            detail = ""
            if isinstance(tool, dict):
                detail = (
                    f"\ntool: {tool.get('name')}"
                    f"\nargs: {json.dumps(tool.get('arguments', {}))[:200]}"
                )
            self._set_panel(
                "action",
                f"action: {payload.get('action')}\nexpected: "
                f"{payload.get('expected_result') or '-'}{detail}",
            )
        elif event.event_type == "tool_result":
            self._set_panel(
                "action",
                f"tool: {payload.get('tool')}\n"
                f"success: {payload.get('success')}\n"
                f"duration: {payload.get('duration_ms')} ms\n"
                f"artifact: {payload.get('artifact') or '-'}",
            )
            self._refresh_git_panel()
        elif event.event_type == "validation":
            passed = payload.get("passed")
            commands = payload.get("command_results") or []
            warnings = payload.get("warnings") or []
            self._set_panel(
                "validation",
                f"passed: {passed}\n"
                f"diff check: {payload.get('diff_check_passed')}\n"
                f"commands: {len(commands)}\n"
                f"changed: {', '.join(payload.get('changed_files') or []) or '-'}\n"
                f"warnings: {'; '.join(warnings) or '-'}",
            )
            self._refresh_git_panel()
        elif event.event_type == "evaluation":
            self.last_evaluation = f"{payload.get('decision')}: {payload.get('reason')}"
            self._log_line(f"[evaluation] {self.last_evaluation}")
        elif event.event_type == "rollback_completed":
            self._log_line("[rollback] speculative state discarded")
        elif event.event_type == "replan":
            self._log_line(f"[replan] {payload.get('reason')}")
        elif event.event_type == "user_override":
            self._log_line(f"[user override] {payload.get('text')}")
        elif event.event_type == "user_question":
            self._log_line(f"[question] {payload.get('question')}")
        elif event.event_type == "run_completed":
            self._log_line("[complete] run finished")
        elif event.event_type == "run_failed":
            self._log_line(f"[failed] {payload.get('reason')}")
        self._refresh()

    # ------------------------------------------------------------------ panels

    def _log_line(self, line: str) -> None:
        stamp = datetime.now(UTC).strftime("%H:%M:%S")
        entry = f"{stamp} {line}"
        self.log_lines.append(entry)
        self.log_lines = self.log_lines[-500:]
        try:
            self.query_one("#log", RichLog).write(entry)
        except NoMatches:
            pass

    def _set_panel(self, key: str, text: str) -> None:
        self.panel_state[key] = text
        try:
            self.query_one(f"#{key}", Static).update(text)
        except NoMatches:
            pass

    def _refresh(self) -> None:
        state = self.runtime.state
        if state is None:
            if self.last_error:
                self._set_panel(
                    "run",
                    f"failed:\n{self.last_error}\n\npress i to enter a new task, q to quit",
                )
            else:
                self._set_panel("run", "waiting for the task description...")
            self._set_panel("objective", "objective: (not set)\nenter the task to start")
            return
        elapsed = datetime.now(UTC) - state.created_at
        self._set_panel(
            "run",
            f"run {state.run_id}  [{state.status}]\n"
            f"phase {state.phase.value}  iteration {state.iteration}\n"
            f"branch {state.branch}\n"
            f"worktree {state.worktree}\n"
            f"elapsed {str(elapsed).split('.')[0]}",
        )
        goal = next(
            (
                step.goal
                for step in state.plan
                if step.status in {"pending", "active"}
            ),
            "none",
        )
        self._set_panel(
            "objective",
            f"objective: {state.objective}\n"
            f"current goal: {goal}\n"
            f"latest instruction: {state.latest_user_instruction or 'none'}",
        )
        plan_lines = [
            f"[{step.status}] {step.id}: {step.goal}" for step in state.plan
        ] or ["(no open steps)"]
        self._set_panel("plan", "plan:\n" + "\n".join(plan_lines))
        self._refresh_git_panel()
        context = self.runtime.last_context_info
        self._set_panel(
            "context",
            f"context limit: {self.runtime.context_limit} tokens\n"
            f"estimated: {context.get('estimated_tokens', 0)} tokens "
            f"({context.get('characters', 0)} chars)\n"
            f"pinned records: {context.get('pinned', 0)}\n"
            f"omitted records: {context.get('omitted', 0)}",
        )
        self._set_panel(
            "model",
            f"provider: {self.runtime.provider.__class__.__name__}\n"
            f"model: {self.runtime.active_model()}\n"
            f"role: controller\n"
            f"last evaluation: {self.last_evaluation}",
        )
        counts = {}
        if self.runtime.memory is not None:
            try:
                counts = self.runtime.memory.counts(state.run_id)
            except Exception:  # noqa: BLE001 - display only
                counts = {}
        self._set_panel(
            "memory",
            "memory (this run):\n"
            + "\n".join(f"{kind}: {total}" for kind, total in sorted(counts.items()))
            if counts
            else "memory (this run): empty",
        )

    def _refresh_git_panel(self) -> None:
        state = self.runtime.state
        if state is None:
            return
        validation = state.latest_validation
        changed = validation.changed_files if validation is not None else []
        self._set_panel(
            "git",
            f"base commit: {state.accepted_commit or 'none'}\n"
            f"accepted steps: {state.accepted_steps}\n"
            f"candidate changes: {len(changed)}\n"
            f"files: {', '.join(changed[:8]) or '-'}",
        )

    # ------------------------------------------------------------------ actions

    def action_quit_app(self) -> None:
        if not self.agent_done:
            self.control.stop()
        self.exit()

    def action_pause(self) -> None:
        self.control.pause()
        self._log_line("[control] paused before next action")

    def action_resume(self) -> None:
        self.control.resume()
        self._log_line("[control] resumed")

    def action_stop(self) -> None:
        self.control.stop()
        self._log_line("[control] stop requested")

    def action_diff(self) -> None:
        diff = ""
        if self.runtime.repo is not None:
            try:
                diff = self.runtime.repo.diff()
            except GitError as exc:
                diff = f"(diff unavailable: {exc})"
        self.push_screen(DiffScreen(diff))

    def action_instruction(self) -> None:
        if self.runtime.state is None or self.agent_done:
            # nothing running: offer the request screen again so failures are recoverable
            self.push_screen(RequestScreen(), self._submit_request)
            return
        self.push_screen(InstructionScreen(), self._submit_instruction)

    def action_toggle_logs(self) -> None:
        self.log_visible = not self.log_visible
        self._apply_layout()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def _submit_instruction(self, text: str | None) -> None:
        if not text or not text.strip():
            return
        if self.agent_done:
            self._log_line("run already finished; instruction ignored")
            return
        self.control.submit_instruction(text.strip())
        self._log_line(f"queued instruction: {text.strip()}")

    def on_unmount(self) -> None:
        if not self.agent_done and self.control is not None:
            self.control.stop()
