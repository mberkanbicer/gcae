"""Full-screen viewers: diff, logs, memory, context, plan and evaluation details."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Label, ListItem, ListView, Static

from ..models import PlanStep, TrajectoryStep
from ..progress import ProgressEvent
from . import formatters
from .formatters import STYLES, elide, short_id

VIEWER_BINDINGS: list[BindingType] = [
    Binding("escape", "close_viewer", "Close", show=False),
    Binding("q", "close_viewer", "Close", show=False),
    Binding("j", "scroll(1)", "Down", show=False),
    Binding("k", "scroll(-1)", "Up", show=False),
]


def _compact(value: object, limit: int = 100) -> str:
    """One bounded line for an event detail value."""
    flat = " ".join(str(value).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


class ViewerScreen(ModalScreen[None]):
    """Common frame: a header line, a scrollable body, a hint line."""

    BINDINGS: ClassVar[list[BindingType]] = list(VIEWER_BINDINGS)

    def __init__(self, title: str, hint: str) -> None:
        super().__init__()
        self.viewer_title = title
        self.viewer_hint = hint
        self.body = Text("")

    def compose(self) -> ComposeResult:
        with Vertical(id="viewer"):
            yield Static(self.viewer_title, id="viewer-title")
            yield from self.compose_body()
            yield Static(self.viewer_hint, id="viewer-hint")

    def compose_body(self) -> ComposeResult:  # pragma: no cover - overridden
        return ()

    def action_close_viewer(self) -> None:
        self.dismiss(None)

    def action_scroll(self, direction: int) -> None:
        target = self.query("#viewer-scroll")
        if not target:
            return
        container = target.first()
        if direction > 0:
            container.scroll_down(animate=False)
        else:
            container.scroll_up(animate=False)

    def set_body(self, body: Text) -> None:
        self.body = body
        self.query_one("#viewer-body", Static).update(body)


class DiffScreen(ViewerScreen):
    """Candidate scope: file list on the left, rendered unified diff on the right."""

    BINDINGS: ClassVar[list[BindingType]] = [
        *VIEWER_BINDINGS,
        Binding("enter", "focus_diff", "Open", show=False),
    ]

    def __init__(self, files: Sequence[dict[str, Any]], reference: str, dirty: bool) -> None:
        state = "candidate DIRTY" if dirty else "candidate CLEAN"
        super().__init__(
            f"diff · {state} · trusted {short_id(reference)}",
            "Enter/j/k switch and scroll files · Esc closes",
        )
        self.files = list(files)

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="diff-body"):
            with VerticalScroll(id="viewer-scroll-list", classes="scrollpane"):
                yield ListView(id="diff-files")
            with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
                yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        listing = self.query_one("#diff-files", ListView)
        if not self.files:
            self.set_body(Text("working tree clean · no candidate changes", style=STYLES["muted"]))
            self.query_one("#viewer-scroll-list", VerticalScroll).display = False
            return
        for entry in self.files:
            added = entry.get("added")
            deleted = entry.get("deleted")
            delta = ""
            if added is not None or deleted is not None:
                added_text = added if added is not None else "-"
                deleted_text = deleted if deleted is not None else "-"
                delta = f" +{added_text} -{deleted_text}"
            label = formatters.file_label(str(entry.get("code")))
            row = Text(f"{label:<2} ", style=STYLES["accent"])
            row.append(elide(str(entry.get("path")), 40))
            row.append(delta, style=STYLES["muted"])
            listing.append(ListItem(Label(row), id=f"diff-file-{len(listing.children)}"))
        listing.index = 0
        self._show(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "diff-files" and event.item is not None:
            self._show(self.query_one("#diff-files", ListView).index or 0)

    def _show(self, index: int) -> None:
        if not self.files or index >= len(self.files):
            return
        entry = self.files[index]
        body = formatters.render_diff(str(entry.get("diff") or ""))
        if entry.get("truncated"):
            body.append("\n… diff truncated in the viewer\n", style=STYLES["warning"])
        self.set_body(body)
        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        scroll.scroll_home(animate=False)

    def action_focus_diff(self) -> None:
        self.query_one("#viewer-scroll", VerticalScroll).focus()


class LogsScreen(ViewerScreen):
    """Detailed runtime log with a small filter cycle."""

    FILTERS: tuple[tuple[str, str], ...] = (
        ("semantic", "__semantic__"),
        ("all", ""),
        ("model", "[model]"),
        ("guardian", "[guardian_check]"),
        ("tools", "[tool]"),
        ("context", "[context]"),
        ("git", "[git]"),
        ("validation", "[validation]"),
        ("evaluation", "[evaluation]"),
        ("errors", "failed"),
    )
    BINDINGS: ClassVar[list[BindingType]] = [
        *VIEWER_BINDINGS,
        Binding("f", "cycle_filter", "Filter", show=False),
    ]

    def __init__(self, lines: Callable[[], list[str]], log_path: str) -> None:
        super().__init__("logs · semantic", f"{log_path} · f cycles filters · Esc closes")
        self._lines = lines
        self.filter_index = 0
        self._at_bottom = True
        self._timer: object | None = None

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        self._reload()
        self._timer = self.set_interval(0.5, self._reload)

    def _reload(self) -> None:
        needle = self.FILTERS[self.filter_index][1]
        lines = self._lines()
        if needle == "__semantic__":
            # default view: engineering progress without model chunk telemetry.
            # Provider milestones (started/finished/stalls) stay; per-chunk progress
            # and raw context dumps move to MODEL/ALL.
            kept: list[str] = []
            for line in lines:
                if "provider_progress" in line or "provider_first_token" in line:
                    continue
                tag = line.split(" ", 1)[1] if " " in line else line
                if tag.startswith("[context]"):
                    continue
                kept.append(line)
            lines = kept
        elif needle:
            lines = [line for line in lines if needle in line]
        body = Text()
        for line in lines[-4000:]:
            style = ""
            if "failed" in line or "×" in line:
                style = STYLES["error"]
            elif "✓" in line or "accepted" in line:
                style = STYLES["success"]
            body.append(line + "\n", style=style)
        if not body:
            body = Text("no matching log entries", style=STYLES["muted"])
        self.set_body(body)
        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        if self._at_bottom:
            scroll.scroll_end(animate=False)

    def action_cycle_filter(self) -> None:
        self.filter_index = (self.filter_index + 1) % len(self.FILTERS)
        name = self.FILTERS[self.filter_index][0]
        self.viewer_title = f"logs · {name}"
        self.query_one("#viewer-title", Static).update(self.viewer_title)
        self._reload()

    def action_scroll(self, direction: int) -> None:
        scroll = self.query_one("#viewer-scroll", VerticalScroll)
        if direction > 0:
            scroll.scroll_down(animate=False)
            self._at_bottom = scroll.scroll_offset.y >= scroll.max_scroll_y - 1
        else:
            scroll.scroll_up(animate=False)
            self._at_bottom = False


class MemoryScreen(ViewerScreen):
    """Stored operational memory by category, with provenance."""

    def __init__(self, records: Sequence[dict[str, Any]]) -> None:
        super().__init__(
            f"memory · {len(records)} records",
            "facts, decisions, failures and instructions stored for this run · Esc closes",
        )
        self.records = list(records)

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        body = Text()
        if not self.records:
            body.append("no memory stored for this run yet\n", style=STYLES["muted"])
        groups: dict[str, list[dict[str, Any]]] = {}
        for record in self.records:
            groups.setdefault(str(record.get("kind") or "other"), []).append(record)
        for kind, items in sorted(groups.items()):
            body.append(f"{kind.upper()} · {len(items)}\n", style=STYLES["title"])
            for record in items:
                marker = "! " if record.get("immutable") else "· "
                style = STYLES["warning"] if record.get("immutable") else STYLES["value"]
                body.append(marker + elide(str(record.get("content")), 96) + "\n", style=style)
                provenance = []
                if record.get("step_id"):
                    provenance.append(str(record["step_id"]))
                if record.get("commit_sha"):
                    provenance.append(short_id(str(record["commit_sha"])))
                if record.get("created_at"):
                    provenance.append(str(record["created_at"])[:19])
                if record.get("source"):
                    provenance.append(str(record["source"]))
                body.append("    " + " · ".join(provenance) + "\n", style=STYLES["muted"])
            body.append("\n")
        self.set_body(body)


class ContextScreen(ViewerScreen):
    """What the model currently receives: section sizes and section content."""

    def __init__(self, text: str, info: dict[str, Any], limit: int) -> None:
        titles = f"{info.get('estimated_tokens', 0)} tok (est) / {limit} budget"
        super().__init__(
            f"context · {titles}",
            "j/k select and scroll sections · Esc closes",
        )
        self.text = text
        self.info = info
        self.sections = formatters.context_sections(text)

    def compose_body(self) -> ComposeResult:
        with Horizontal(id="context-body"):
            with VerticalScroll(id="viewer-scroll-list", classes="scrollpane"):
                yield ListView(id="context-sections")
            with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
                yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        listing = self.query_one("#context-sections", ListView)
        if not self.sections:
            self.set_body(Text("no request payload has been built yet", style=STYLES["muted"]))
            self.query_one("#viewer-scroll-list", VerticalScroll).display = False
            return
        total = sum(size for _, size, _, _ in self.sections) or 1
        for name, size, _, _ in self.sections:
            share = int(round(100 * size / total))
            row = Text(f"{name:<22}", style=STYLES["value"])
            row.append(f"{share:>3}%", style=STYLES["muted"])
            listing.append(ListItem(Label(row), id=None))
        listing.index = 0
        self._show(0)

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "context-sections" and event.item is not None:
            self._show(self.query_one("#context-sections", ListView).index or 0)

    def _show(self, index: int) -> None:
        if not self.sections or index >= len(self.sections):
            return
        name, size, _, content = self.sections[index]
        body = Text()
        body.append(f"{name} · {size} chars\n\n", style=STYLES["title"])
        for line in content.splitlines():
            body.append(elide(line, 400) + "\n")
        self.set_body(body)


class TrajectoryScreen(ViewerScreen):
    """Semantic attempts: plan steps, trajectory verdicts and progress lines."""

    def __init__(
        self,
        steps: Sequence[PlanStep],
        trajectory: Sequence[TrajectoryStep],
        progress: Sequence[ProgressEvent],
    ) -> None:
        super().__init__(
            f"trajectory · {len(trajectory)} attempts",
            "attempts with verdicts and knowledge · Esc closes",
        )
        self.steps = list(steps)
        self.trajectory = list(trajectory)
        self.progress = list(progress)

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        body = Text()
        if self.steps:
            body.append("PLAN\n", style=STYLES["title"])
            for step in self.steps:
                marker, style = formatters.plan_marker(step.status)
                body.append(f"{marker} {step.id} · {step.goal}\n", style=STYLES[style])
                if step.rationale:
                    body.append(f"    why      {step.rationale[:90]}\n", style=STYLES["muted"])
            body.append("\n")
        body.append("ATTEMPTS\n", style=STYLES["title"])
        if not self.trajectory:
            body.append("no attempts yet\n", style=STYLES["muted"])
        for attempt in self.trajectory:
            raw_status = attempt.status
            status = str(raw_status.value if hasattr(raw_status, "value") else raw_status)
            marker = {
                "accepted": "✓", "rejected": "×", "repaired": "↻",
                "replanned": "↻", "blocked": "!",
            }.get(status, "●")
            body.append(f"{marker} {attempt.id} · {attempt.semantic_goal}\n")
            if attempt.expectation:
                body.append(f"    expected {attempt.expectation[:90]}\n", style=STYLES["muted"])
            verdict = attempt.decision_reason or attempt.decision
            if verdict:
                body.append(f"    verdict  {verdict[:90]}\n", style=STYLES["muted"])
            for lesson in list(attempt.knowledge_gained or [])[:2]:
                body.append(f"    learned  {str(lesson)[:90]}\n", style=STYLES["muted"])
            if attempt.evidence_ids:
                count = len(attempt.evidence_ids)
                body.append(f"    evidence {count} records\n", style=STYLES["muted"])
            body.append("\n")
        self.set_body(body)


class EventDetailScreen(ViewerScreen):
    """Level 2 for one semantic event: command, outcome, evidence — no telemetry.

    j/k walk the run's progress events (newest first). Everything shown comes
    from the event's own payload; raw provider output stays in Logs.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close_viewer", "Close", show=False),
        Binding("q", "close_viewer", "Close", show=False),
        Binding("j", "step(1)", "Older", show=False),
        Binding("k", "step(-1)", "Newer", show=False),
    ]

    def __init__(self, events: Sequence[ProgressEvent]) -> None:
        super().__init__("event detail", "j/k step through events · Esc closes")
        self.events = list(reversed(list(events)))
        self.index = 0

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        self._show()

    def action_step(self, delta: int) -> None:
        if self.events:
            self.index = max(0, min(len(self.events) - 1, self.index + delta))
            self._show()

    def _show(self) -> None:
        if not self.events:
            self.set_body(Text("no semantic events yet"))
            return
        event = self.events[self.index]
        body = Text()
        body.append(f"{self.index + 1}/{len(self.events)}\n", style=STYLES["muted"])
        body.append(
            f"{event.category.value.upper()}  {event.title}\n", style=STYLES["value"]
        )
        if event.result:
            body.append(f"result    {event.result}\n")
        body.append(
            f"at        {event.timestamp.astimezone().strftime('%H:%M:%S')}\n",
            style=STYLES["muted"],
        )
        if event.trajectory_step_id:
            body.append(f"step      {event.trajectory_step_id}\n")
        if event.related_command:
            body.append(f"command   {event.related_command}\n")
        if event.related_file:
            body.append(f"file      {event.related_file}\n")
        if event.evidence_ids:
            ids = ", ".join(f"E{identifier}" for identifier in event.evidence_ids)
            body.append(f"evidence  {ids}\n")
        if event.severity != "info":
            body.append(f"severity  {event.severity}\n", style=STYLES["warning"])
        for key, value in (event.detail or {}).items():
            body.append(f"{key:<9} {_compact(value)}\n", style=STYLES["muted"])
        self.set_body(body)


class HealthScreen(ViewerScreen):
    """Guardian health from real checks: one row per subsystem, no green paint."""

    def __init__(self, checks: Sequence[tuple[str, str, str]]) -> None:
        super().__init__("health", "health detail per subsystem · Esc closes")
        self.checks = list(checks)

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        body = Text()
        for component, state, detail in self.checks:
            marker = "✓" if state == "ok" else "!" if state == "degraded" else "×"
            line = f"{marker} {component} · {state}"
            if detail:
                line += f" · {detail[:80]}"
            body.append(line + "\n")
        self.set_body(body)


class EvaluationScreen(ViewerScreen):
    """Latest evaluation decision, verification evidence and validation detail."""

    def __init__(
        self,
        evaluation: dict[str, Any] | None,
        verification: dict[str, Any] | None,
        validation: dict[str, Any] | None,
    ) -> None:
        super().__init__("evaluation", "structured operational reasoning · Esc closes")
        self.evaluation = evaluation
        self.verification = verification
        self.validation = validation

    def compose_body(self) -> ComposeResult:
        with VerticalScroll(id="viewer-scroll", classes="scrollpane"):
            yield Static("", id="viewer-body")

    def on_mount(self) -> None:
        body = Text()
        evaluation = self.evaluation
        if evaluation is None:
            body.append("no evaluation yet\n", style=STYLES["muted"])
        else:
            decision = str(evaluation.get("decision"))
            style = {
                "accept": "success",
                "rollback": "warning",
                "replan": "warning",
                "continue": "muted",
            }.get(decision, "value")
            body.append("DECISION  ", style=STYLES["title"])
            body.append(f"{decision}\n", style=f"bold {STYLES[style]}")
            body.append("REASON    ", style=STYLES["title"])
            body.append(f"{evaluation.get('reason') or '-'}\n")
            if evaluation.get("next_goal"):
                body.append("NEXT      ", style=STYLES["title"])
                body.append(f"{evaluation['next_goal']}\n")
            promoted = evaluation.get("memories_to_promote") or []
            body.append("MEMORY    ", style=STYLES["title"])
            body.append(f"{len(promoted)} promoted\n", style=STYLES["value"])
            for candidate in promoted[:6]:
                record = candidate.get("record") or {}
                body.append(
                    f"    {record.get('kind')}: {elide(str(record.get('content')), 90)}\n",
                    style=STYLES["muted"],
                )
            body.append("\n")
        verification = self.verification
        body.append("VERIFICATION\n", style=STYLES["title"])
        if verification is None:
            body.append("    not run yet\n", style=STYLES["muted"])
        else:
            for item in verification.get("criteria") or []:
                ok = bool(item.get("passed"))
                body.append(
                    f"    {'✓' if ok else '×'} {item.get('criterion')}\n",
                    style=STYLES["success" if ok else "error"],
                )
                if item.get("evidence"):
                    evidence = elide(str(item["evidence"]), 110)
                    body.append(f"        {evidence}\n", style=STYLES["muted"])
            if verification.get("details"):
                body.append(
                    f"    details: {verification['details']}\n", style=STYLES["muted"]
                )
        validation = self.validation
        body.append("\nVALIDATION\n", style=STYLES["title"])
        if validation is None:
            body.append("    not run yet\n", style=STYLES["muted"])
        else:
            commands = validation.get("commands") or []
            for index, result in enumerate(validation.get("command_results") or []):
                ok = bool(result.get("success"))
                label = commands[index] if index < len(commands) else result.get("tool")
                body.append(
                    f"    {'✓' if ok else '×'} {label} (exit {result.get('exit_code')})\n",
                    style=STYLES["success" if ok else "error"],
                )
                if not ok and result.get("output"):
                    tail = str(result["output"]).strip().splitlines()[-3:]
                    for line in tail:
                        body.append(f"        {elide(line, 110)}\n", style=STYLES["muted"])
            body.append(
                f"    diff check {'passed' if validation.get('diff_check_passed') else 'failed'}\n",
                style=STYLES["muted"],
            )
            for warning in validation.get("warnings") or []:
                body.append(f"    warn: {elide(str(warning), 100)}\n", style=STYLES["warning"])
        self.set_body(body)
