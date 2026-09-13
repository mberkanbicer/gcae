"""GCAE dashboard.

The app owns no runtime truth: it renders ``runtime.state`` plus the presentation
reducer in :mod:`gcae.tui.state`, runs the agent in a worker thread, and performs the
few remaining slow reads (git diff, git status, memory listing) off the UI thread.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widgets import Static
from textual.worker import Worker

from ..git import GitError, NothingToMerge
from ..models import Event
from ..runtime import Runtime, RuntimeControl
from . import formatters
from .modals import ConfirmStopModal, HelpModal, InstructionModal, RequestModal
from .screens import (
    ContextScreen,
    DiffScreen,
    EvaluationScreen,
    LogsScreen,
    MemoryScreen,
    PlanScreen,
)
from .state import PANELS, UiState
from .widgets import (
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

WIDTH_NORMAL = 100
WIDTH_LARGE = 140
HEIGHT_TALL = 40
HEIGHT_SHORT = 30


class GcaeApp(App[None]):
    """Live dashboard for a single GCAE run."""

    TITLE = "GCAE"
    SUB_TITLE = "Git-Checkpointed Adaptive Execution"
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        Binding("q", "quit_app", "Quit"),
        Binding("p", "pause", "Pause"),
        Binding("r", "resume", "Resume"),
        Binding("s", "stop", "Stop"),
        Binding("d", "diff", "Diff"),
        Binding("l", "logs", "Logs"),
        Binding("m", "memory", "Memory"),
        Binding("c", "context", "Context"),
        Binding("e", "evaluation", "Evaluation"),
        Binding("t", "plan_detail", "Plan"),
        Binding("i", "instruction", "Instruct"),
        Binding("M", "merge_run", "Merge"),
        Binding("question_mark", "help", "Help"),
        Binding("enter", "inspect", "Open"),
        Binding("tab", "focus_next_panel", "Next panel", show=False),
        Binding("shift+tab", "focus_previous_panel", "Previous panel", show=False),
        Binding("j", "focus_next_panel", "Next panel", show=False),
        Binding("k", "focus_previous_panel", "Previous panel", show=False),
    ]

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
        self.ui = UiState(request=request)
        self.last_error: str | None = None
        self.checkpoint_subject = ""
        self.agent_done = False
        self.focus_order = ["plan", "checkpoint", "validation", "activity", "objective", "timeline"]
        self.focus_index = 0
        self._needs_start = request is not None and runtime.state is None
        self._merge_attempted = False
        self.merge_error: str | None = None
        self._agent: Worker[None] | None = None

    # ------------------------------------------------------------------ layout

    def compose(self) -> ComposeResult:
        yield StatusBar()
        with Vertical(id="main") as main:
            main.set_class(False, "stacked")
            with Vertical(id="column-left"):
                yield ObjectivePanel()
                yield PlanPanel()
            with Vertical(id="column-right"):
                yield ActivityPanel()
                yield CheckpointPanel()
                yield ValidationPanel()
        yield BannerPanel()
        yield MetricsPanel()
        yield Static("", id="rule-top", classes="rule")
        yield TimelinePanel()
        yield Static("", id="rule-bottom", classes="rule")
        yield FooterBar()

    def on_mount(self) -> None:
        self.runtime.subscribe(self._on_event_threadsafe)
        state = self.runtime.state
        if state is not None:
            self.ui.seed_from_state(state)
        self.set_interval(0.5, self._tick)
        self._apply_responsive()
        self._refresh_panels(set(PANELS))
        self._refresh_git_state()
        if state is None and self.request is None:
            self._prompt_for_request()
        elif self.auto_run:
            self._launch_agent()
        try:
            self.query_one(PlanPanel).focus()
        except NoMatches:  # pragma: no cover - composition failure would be fatal anyway
            pass

    def on_resize(self) -> None:
        self._apply_responsive()
        self._refresh_panels(set(PANELS))

    def _apply_responsive(self) -> None:
        width = self.size.width
        height = self.size.height
        try:
            main = self.query_one("#main", Vertical)
            metrics = self.query_one(MetricsPanel)
            timeline = self.query_one(TimelinePanel)
            plan = self.query_one(PlanPanel)
            checkpoint = self.query_one(CheckpointPanel)
            validation = self.query_one(ValidationPanel)
        except NoMatches:  # pragma: no cover - resize before composition
            return
        main.set_class(width < WIDTH_NORMAL, "stacked")
        timeline.set_class(width < WIDTH_NORMAL, "compact")
        metrics.display = width >= 90
        timeline.display = height >= 20
        stacked = width < WIDTH_NORMAL
        if stacked:
            timeline.rows = 3
        elif height >= HEIGHT_TALL:
            timeline.rows = 16 if width >= WIDTH_LARGE else 12
        elif height >= HEIGHT_SHORT:
            timeline.rows = 8
        else:
            timeline.rows = 4
        plan.max_rows = 5 if stacked or height < HEIGHT_SHORT else 10
        checkpoint.max_files = 4 if height >= HEIGHT_SHORT else 2
        validation.max_rows = 4 if height < HEIGHT_SHORT else 8
        rule = "─" * max(10, width)
        for rule_id in ("rule-top", "rule-bottom"):
            rule_widget = self.query_one(f"#{rule_id}", Static)
            rule_widget.update(Text(rule, style=formatters.STYLES["rule"]))

    # ------------------------------------------------------------------ agent worker

    def _launch_agent(self) -> None:
        self.agent_done = False
        self.last_error = None
        self._agent = self.run_worker(
            self._run_agent, thread=True, name="agent", exit_on_error=False
        )

    def _run_agent(self) -> None:
        try:
            if self._needs_start:
                assert self.request is not None
                self._needs_start = False
                self.runtime.start(
                    self.request,
                    hard_constraints=self.constraints,
                    success_criteria=self.criteria,
                )
                self._call_ui(self._adopt_state)
            self.runtime.run()
        except Exception as exc:  # noqa: BLE001 - surfaced in the dashboard
            self._call_ui(self._on_agent_error, str(exc))
        finally:
            self._call_ui(self._on_agent_finished)

    def _adopt_state(self) -> None:
        state = self.runtime.state
        if state is None:
            return
        self.ui.seed_from_state(state)
        self._refresh_panels({"objective", "plan", "status", "checkpoint", "metrics"})

    def _on_agent_error(self, message: str) -> None:
        self.last_error = message
        self.ui.last_error = message
        self.ui.action = None
        self._refresh_panels({"banner", "status", "activity", "footer"})

    def _on_agent_finished(self) -> None:
        self.agent_done = True
        self.ui.agent_done = True
        self._refresh_git_state()
        self._refresh_panels(set(PANELS))

    # ------------------------------------------------------------------ events

    def _call_ui(self, callback: Callable[..., None], *args: Any) -> None:
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            callback(*args)

    def _on_event_threadsafe(self, event: Event) -> None:
        self._call_ui(self._consume_event, event)

    def _consume_event(self, event: Event) -> None:
        changed = self.ui.apply(event)
        if event.event_type == "checkpoint_created":
            message = str(event.payload.get("message") or "")
            if message:
                self.checkpoint_subject = message
        if event.event_type == "run_failed":
            self.last_error = str(event.payload.get("reason") or "run failed")
        if event.event_type in {"run_completed", "run_failed", "run_stopped"}:
            self.agent_done = True
        auto_merge_due = (
            event.event_type == "run_completed"
            and self.runtime.auto_merge
            and not self._merge_attempted
        )
        if auto_merge_due:
            # merging changes the user's checkout: do it off the UI thread and report it
            self._merge_attempted = True
            self.run_worker(self._merge_task, thread=True, name="auto-merge", exit_on_error=False)
        self._refresh_panels({name for name in changed if name in PANELS})

    # ------------------------------------------------------------------ rendering

    def _tick(self) -> None:
        self._refresh_panels(
            {"status", "activity", "checkpoint", "metrics", "timeline", "banner", "footer"}
        )

    def _refresh_panels(self, panels: set[str]) -> None:
        state = self.runtime.state
        ui = self.ui
        for name in panels:
            if name == "status":
                self.query_one(StatusBar).render_state(
                    state,
                    ui,
                    provider=self._provider_label(),
                    model=self._model_label(),
                    paused=self.control.paused,
                )
            elif name == "objective":
                self.query_one(ObjectivePanel).render_state(state, ui)
            elif name == "plan":
                self.query_one(PlanPanel).render_state(state, ui)
            elif name == "activity":
                self.query_one(ActivityPanel).render_state(state, ui)
            elif name == "checkpoint":
                self.query_one(CheckpointPanel).render_state(
                    state, ui, subject=self._checkpoint_subject()
                )
            elif name == "validation":
                self.query_one(ValidationPanel).render_state(state, ui)
            elif name == "metrics":
                self.query_one(MetricsPanel).render_state(
                    state, ui, limit=self.runtime.context_limit
                )
            elif name == "timeline":
                self.query_one(TimelinePanel).render_state(ui)
            elif name == "banner":
                banner = self.query_one(BannerPanel)
                banner.display = banner.render_state(state, ui)
            elif name == "footer":
                self.query_one(FooterBar).render_text(self._footer_text())

    def _model_label(self) -> str:
        role = self.ui.role.lower()
        mapping = {
            "plan": ("planner", "controller"),
            "act": ("controller",),
            "eval": ("evaluator", "controller"),
            "verify": ("verifier", "controller"),
        }
        for candidate in mapping.get(role, ("controller",)):
            model = self.runtime.role_model(candidate)
            if model:
                return str(model)
        return str(self.runtime.active_model())

    def _provider_label(self) -> str:
        return self.runtime.provider_label or self.runtime.provider.__class__.__name__.lower()

    def _checkpoint_subject(self) -> str:
        if self.checkpoint_subject:
            return self.checkpoint_subject
        return "base commit" if self.runtime.state is not None else ""

    def _footer_text(self) -> str:
        if self.agent_done:
            state = self.runtime.state
            merge_keys = (
                "[M] Merge  "
                if state is not None and state.status == "complete" and state.merge is None
                else ""
            )
            keys = (
                f"[i] New task  {merge_keys}[d] Diff  [l] Logs  [m] Memory  [c] Context  "
                "[e] Evaluation  [t] Plan  [?] Help  [q] Quit"
            )
        elif self.control.paused:
            keys = "[r] Resume  [d] Diff  [l] Logs  [m] Memory  [i] Instruct  [?] Help  [q] Quit"
        elif self.control.stopped:
            keys = "[i] New task  [l] Logs  [?] Help  [q] Quit"
        else:
            keys = (
                "[p] Pause  [s] Stop  [d] Diff  [l] Logs  [m] Memory  [c] Context  "
                "[i] Instruct  [?] Help  [q] Quit"
            )
        return formatters.elide(keys, max(20, self.size.width))

    # ------------------------------------------------------------------ slow reads

    def _refresh_git_state(self) -> None:
        """Read git state off the UI thread; the UI never blocks on git."""
        if self.runtime.repo is None or self.runtime.repo.worktree is None:
            return
        self.run_worker(self._git_state_task, thread=True, name="git-state", exit_on_error=False)

    def _git_state_task(self) -> None:
        repo = self.runtime.repo
        assert repo is not None
        payload: dict[str, Any] = {"subject": ""}
        try:
            payload["snapshot"] = repo.candidate_snapshot()
            payload["subject"] = repo.head_subject()
        except GitError as exc:
            payload["snapshot"] = {
                "dirty": False,
                "files": [],
                "added": 0,
                "deleted": 0,
                "error": str(exc),
            }
        self._call_ui(self._apply_git_state, payload)

    def _apply_git_state(self, payload: dict[str, Any]) -> None:
        snapshot = payload.get("snapshot")
        if isinstance(snapshot, dict):
            self.ui.candidate = snapshot
        subject = str(payload.get("subject") or "")
        if subject:
            self.checkpoint_subject = subject
        self._refresh_panels({"checkpoint", "metrics"})

    def _show_memory(self, records: list[dict[str, Any]]) -> None:
        self.push_screen(MemoryScreen(records))

    def _diff_task(self) -> None:
        repo = self.runtime.repo
        state = self.runtime.state
        assert repo is not None
        payload: dict[str, Any] = {"files": [], "reference": "", "dirty": False, "error": ""}
        try:
            payload["files"] = repo.diff_by_file()
            payload["dirty"] = any(entry.get("diff") for entry in payload["files"])
        except GitError as exc:
            payload["error"] = str(exc)
        if state is not None:
            payload["reference"] = state.accepted_commit or ""
        self._call_ui(self._show_diff, payload)

    def _show_diff(self, payload: dict[str, Any]) -> None:
        self.push_screen(
            DiffScreen(
                payload.get("files") or [],
                str(payload.get("reference") or ""),
                bool(payload.get("dirty")),
            )
        )

    def _memory_task(self) -> None:
        memory = self.runtime.memory
        state = self.runtime.state
        records: list[dict[str, Any]] = []
        if memory is not None and state is not None:
            try:
                records = [record.model_dump(mode="json") for record in memory.all(state.run_id)]
            except Exception:  # noqa: BLE001 - a memory read failure must not kill the UI
                records = []
        self._call_ui(self._show_memory, records)

    # ------------------------------------------------------------------ actions

    def action_merge_run(self) -> None:
        state = self.runtime.state
        if state is None or state.status != "complete" or state.merge is not None:
            return
        self.run_worker(self._merge_task, thread=True, name="merge", exit_on_error=False)

    def _merge_task(self) -> None:
        try:
            record = self.runtime.merge_completed_run()
        except NothingToMerge as exc:
            self._call_ui(self._on_nothing_to_merge, str(exc))
            return
        except (GitError, RuntimeError) as exc:
            self._call_ui(self._on_merge_failed, str(exc))
            return
        self._call_ui(self._on_merged, record)

    def _on_nothing_to_merge(self, reason: str) -> None:
        self.ui.add_note("i", reason, "muted")
        self._refresh_panels({"banner", "footer", "timeline"})

    def _on_merged(self, record: object) -> None:
        target = getattr(record, "target_branch", "?")
        commit = str(getattr(record, "merge_commit", ""))[:7]
        self.ui.add_note("✓", f"merged into {target} · {commit} · gcae undo reverses it", "success")
        self._refresh_panels({"banner", "footer", "timeline", "checkpoint"})

    def _on_merge_failed(self, reason: str) -> None:
        self.merge_error = reason
        self.ui.add_note("!", f"merge skipped · {reason}", "warning")
        self._refresh_panels({"banner", "footer", "timeline"})

    def action_quit_app(self) -> None:
        if not self.agent_done:
            self.control.stop()
        self.exit()

    def action_pause(self) -> None:
        self.control.pause()
        self._refresh_panels({"status", "activity", "footer"})

    def action_resume(self) -> None:
        self.control.resume()
        self._refresh_panels({"status", "footer"})

    def action_stop(self) -> None:
        if self.agent_done:
            self._refresh_panels({"status", "activity", "footer"})
            return
        self.push_screen(ConfirmStopModal(), self._confirm_stop)

    def _confirm_stop(self, result: object) -> None:
        if result is not True:
            return
        self.control.stop()
        self._refresh_panels({"status", "activity", "footer"})

    def action_diff(self) -> None:
        if self.runtime.state is None:
            return
        self.run_worker(self._diff_task, thread=True, name="diff", exit_on_error=False)

    def action_logs(self) -> None:
        path = str(getattr(self.runtime.events, "path", "") or "")
        self.push_screen(LogsScreen(lambda: list(self.ui.logs), path))

    def action_memory(self) -> None:
        self.run_worker(self._memory_task, thread=True, name="memory", exit_on_error=False)

    def action_context(self) -> None:
        self.push_screen(
            ContextScreen(
                self.runtime.last_context_text,
                dict(self.runtime.last_context_info or {}),
                self.runtime.context_limit,
            )
        )

    def action_evaluation(self) -> None:
        self.push_screen(
            EvaluationScreen(self.ui.evaluation, self.ui.verification, self.ui.validation)
        )

    def action_plan_detail(self) -> None:
        state = self.runtime.state
        steps = state.plan if state is not None else []
        self.push_screen(PlanScreen(steps, self.ui.plan_reason))

    def action_help(self) -> None:
        self.push_screen(HelpModal())

    def action_inspect(self) -> None:
        focused = self.focused
        panel_id = getattr(focused, "id", None) or "plan"
        if panel_id == "checkpoint":
            self.action_diff()
        elif panel_id in {"validation", "activity"}:
            self.action_evaluation()
        elif panel_id == "objective":
            self.action_context()
        elif panel_id == "timeline":
            self.action_logs()
        else:
            self.action_plan_detail()

    def action_focus_next_panel(self) -> None:
        self.focus_index = (self.focus_index + 1) % len(self.focus_order)
        self._focus_panel()

    def action_focus_previous_panel(self) -> None:
        self.focus_index = (self.focus_index - 1) % len(self.focus_order)
        self._focus_panel()

    def _focus_panel(self) -> None:
        for offset in range(len(self.focus_order)):
            name = self.focus_order[(self.focus_index + offset) % len(self.focus_order)]
            try:
                widget = self.query_one(f"#{name}")
            except NoMatches:
                continue
            if widget.display:
                widget.focus()
                return

    def action_instruction(self) -> None:
        if self.runtime.state is None or self.agent_done:
            self._prompt_for_request()
            return
        self.push_screen(InstructionModal(), self._submit_instruction)

    def _prompt_for_request(self) -> None:
        self.push_screen(RequestModal(), self._submit_request)

    def _submit_request(self, text: object) -> None:
        if not isinstance(text, str) or not text.strip():
            self._refresh_panels({"status", "footer"})
            return
        timeline = self.ui.timeline
        self.ui = UiState(request=text.strip())
        self.ui.timeline = timeline
        self.request = text.strip()
        self.last_error = None
        self.checkpoint_subject = ""
        self.agent_done = False
        self._needs_start = True
        self.ui.add_note("i", f"task accepted · {formatters.elide(self.request, 70)}", "accent")
        self._refresh_panels(set(PANELS))
        if self.auto_run:
            self._launch_agent()

    def _submit_instruction(self, text: object) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        instruction = text.strip()
        self.control.submit_instruction(instruction)
        self.ui.add_note("i", f"instruction queued · {formatters.elide(instruction, 70)}", "accent")
        self._refresh_panels({"objective", "timeline", "activity"})

    def on_unmount(self) -> None:
        if not self.agent_done:
            self.control.stop()
