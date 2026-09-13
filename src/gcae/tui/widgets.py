"""Dashboard widgets.

Each widget owns one information zone and renders it from :class:`UiState` plus the
authoritative ``AgentState``.  Widgets are display-only: they never call git, the
database or the provider.
"""

from __future__ import annotations

import textwrap
from datetime import UTC, datetime
from typing import Any

from rich.text import Text
from textual.widgets import Static

from ..models import AgentState
from . import formatters
from .formatters import STYLES, duration, elide, short_id
from .state import UiState

LABEL_WIDTH = 12


def _row(label: str, content: Text | str, *, label_style: str = "label") -> Text:
    row = Text(f"{label:<{LABEL_WIDTH}}", style=STYLES[label_style])
    if isinstance(content, Text):
        row.append_text(content)
    else:
        row.append(content)
    return row


def _wrap(text: str, width: int, rows: int) -> list[str]:
    if width <= 4 or rows <= 0:
        return []
    lines = textwrap.wrap(" ".join(text.split()), width=width) or [""]
    if len(lines) > rows:
        lines = lines[:rows]
        lines[-1] = elide(lines[-1] + "…", width)
    return lines


def _window(items: list[Any], active: int, rows: int) -> tuple[list[tuple[int, Any]], int, int]:
    """Visible (index, item) rows around the active item, plus hidden counts."""
    if len(items) <= rows:
        return list(enumerate(items)), 0, 0
    half = max(0, (rows - 1) // 2)
    start = max(0, min(active - half, len(items) - rows))
    end = start + rows
    return list(enumerate(items))[start:end], start, len(items) - end


class Panel(Static):
    """Titled block: one dim uppercase title row plus body rows. No border."""

    can_focus = True

    def __init__(self, title: str, id: str) -> None:
        super().__init__("", id=id, markup=False)
        self.panel_title = title
        self.body = Text("")

    @property
    def content_width(self) -> int:
        """Panel width, falling back to the screen width before the first layout."""
        width = self.size.width
        if width <= 24:
            try:
                width = self.app.size.width - 2
            except Exception:  # noqa: BLE001 - not mounted yet
                width = 78
        return max(20, width)

    def set_body(self, body: Text) -> None:
        """Render Rich text and keep a readable copy for tests and the demo."""
        self.body = body
        self.update(body)

    def render_block(self, meta: str, lines: list[Text]) -> None:
        width = self.content_width
        body = Text()
        if self.panel_title:
            header = Text(self.panel_title.upper(), style=STYLES["title"])
            if meta:
                header = formatters.pad_row(header, Text(meta, style=STYLES["muted"]), width)
            body.append_text(header)
            if lines:
                body.append("\n")
        for index, line in enumerate(lines):
            if index:
                body.append("\n")
            body.append_text(line)
        self.set_body(body)


class StatusBar(Panel):
    """One-line run identity: app, state, project, run, model role, elapsed."""

    def __init__(self) -> None:
        super().__init__("", id="status")

    def render_state(
        self,
        state: AgentState | None,
        ui: UiState,
        *,
        provider: str,
        model: str,
        paused: bool,
    ) -> None:
        width = self.content_width
        status = state.status if state is not None else "starting"
        badge, badge_style = formatters.run_badge(status, paused)
        row = Text("GCAE", style="bold")
        row.append(" │ ", style=STYLES["muted"])
        row.append(badge, style=f"bold {STYLES[badge_style]}")
        active = state is None or not str(status).startswith(("complete", "failed", "stopped"))
        if ui.rollback_active() and active:
            row.append(" │ ROLLBACK", style=f"bold {STYLES['warning']}")
        if paused:
            row.append(" │ paused · no new model or tool action", style=STYLES["warning"])
        segments: list[tuple[str, str]] = []
        if state is not None and width >= 70:
            project = state.source_repo.rstrip("/").split("/")[-1]
            segments.append((project, STYLES["value"]))
        if state is not None:
            segments.append((short_id(state.run_id, 6), STYLES["muted"]))
        model_text = elide(model, 26)
        if provider and provider.lower() not in model.lower():
            model_text = f"{provider} · {model_text}"
        if ui.role and width >= 104:
            model_text = f"{model_text} · {ui.role}"
        segments.append((model_text, STYLES["value"]))
        if state is not None and ui.started_at is not None and state.status != "complete":
            elapsed = (datetime.now(UTC) - ui.started_at).total_seconds()
            if width >= 120:
                segments.append((duration(elapsed), STYLES["muted"]))
        for text, style in segments:
            row.append(" │ ", style=STYLES["muted"])
            row.append(elide(text, max(8, width // 3)), style=style)
        self.set_body(formatters.pad_row(row, Text(""), width))


class BannerPanel(Panel):
    """Completion, stop or failure summary built from real run aggregates."""

    def __init__(self) -> None:
        super().__init__("", id="banner")

    def render_state(self, state: AgentState | None, ui: UiState) -> bool:
        width = self.content_width
        lines: list[Text] = []
        title = Text("")
        if ui.last_error and (state is None or state.status.startswith("failed")):
            title = Text("RUN FAILED", style=f"bold {STYLES['error']}")
            for chunk in _wrap(ui.last_error, width - LABEL_WIDTH, 3):
                lines.append(_row("reason", chunk))
            if state is not None and state.accepted_commit:
                lines.append(
                    _row(
                        "trusted",
                        Text(
                            f"{short_id(state.accepted_commit)} · accepted work is safe on "
                            f"{state.branch}",
                            style=STYLES["success"],
                        ),
                    )
                )
            if ui.current_goal:
                lines.append(_row("goal", elide(ui.current_goal, width - LABEL_WIDTH)))
            hint = Text("press i to describe a new task, q to quit", style=STYLES["muted"])
            lines.append(_row("next", hint))
        elif state is not None and state.status == "complete":
            title = Text("RUN COMPLETE", style=f"bold {STYLES['success']}")
            verification = ui.verification or {}
            criteria = verification.get("criteria") or []
            passed = sum(1 for item in criteria if item.get("passed"))
            def count(total: int, noun: str) -> str:
                return f"{total} {noun}" if total == 1 else f"{total} {noun}s"

            lines.append(
                _row(
                    "result",
                    f"{count(state.accepted_steps, 'accepted step')} · "
                    f"{count(ui.rollbacks, 'rollback')} · {count(ui.replans, 'replan')}",
                )
            )
            lines.append(
                _row(
                    "final",
                    Text(
                        f"{short_id(state.accepted_commit)} · branch {state.branch}",
                        style=STYLES["success"],
                    ),
                )
            )
            if criteria:
                lines.append(_row("criteria", f"{passed}/{len(criteria)} passed"))
            merge_hint = Text(
                "never automatic · run `gcae merge` when ready", style=STYLES["muted"]
            )
            lines.append(_row("merge", merge_hint))
        elif state is not None and state.status == "stopped":
            title = Text("RUN STOPPED", style=f"bold {STYLES['muted']}")
            lines.append(_row("trusted", f"{short_id(state.accepted_commit)} on {state.branch}"))
        if not title.plain:
            self.set_body(Text(""))
            return False
        body = formatters.pad_row(title, Text(""), width)
        for line in lines:
            body.append("\n")
            body.append_text(line)
        self.set_body(body)
        return True


class ObjectivePanel(Panel):
    """Original objective and the goal being worked on now."""

    def __init__(self) -> None:
        super().__init__("objective", id="objective")

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        objective = state.objective if state is not None else (ui.request or "")
        goal = "" if ui.agent_done else ui.current_goal
        meta = ""
        if state is not None:
            meta = (
                f"{len(state.success_criteria)} criteria · "
                f"{len(state.hard_constraints)} constraints"
            )
        if not objective:
            empty = Text("no task yet · press i to describe one", style=STYLES["muted"])
            self.render_block(meta, [empty])
            return
        lines: list[Text] = []
        for index, chunk in enumerate(_wrap(objective, width - LABEL_WIDTH, 2)):
            lines.append(
                _row("original" if index == 0 else "", Text(chunk, style=STYLES["value"]))
            )
        if goal:
            label = "NOW" if ui.goal_is_active else "NEXT"
            style = f"bold {STYLES['accent']}" if ui.goal_is_active else STYLES["value"]
            for index, chunk in enumerate(_wrap(goal, width - LABEL_WIDTH, 2)):
                lines.append(_row(label if index == 0 else "", Text(chunk, style=style)))
        if (
            state is not None
            and state.latest_user_instruction
            and state.latest_user_instruction != state.original_request
        ):
            lines.append(
                _row("instruction", elide(state.latest_user_instruction, width - LABEL_WIDTH))
            )
        self.render_block(meta, lines)


class PlanPanel(Panel):
    """Active execution roadmap: completed steps fade, the current step stands out."""

    def __init__(self) -> None:
        super().__init__("plan", id="plan")
        self.max_rows = 9

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        steps = ui.plan
        if not steps and state is not None:
            steps = [
                {"id": step.id, "goal": step.goal, "status": step.status} for step in state.plan
            ]
        if steps and steps is not ui.plan:
            accepted = state.accepted_steps if state is not None else 0
            done, total = accepted, accepted + len(steps)
        else:
            done, total = ui.plan_position()
        meta = f"{done}/{total} steps" if total or steps else ""
        if not steps:
            message = "no open steps" if ui.agent_done else "planning…"
            self.render_block(meta, [Text(message, style=STYLES["muted"])])
            return
        active = next(
            (index for index, step in enumerate(steps) if step.get("status") == "active"), 0
        )
        visible, hidden_before, hidden_after = _window(steps, active, max(2, self.max_rows))
        lines: list[Text] = []
        if hidden_before:
            lines.append(Text(f"… {hidden_before} completed", style=STYLES["muted"]))
        goal_width = max(10, width - 4)
        for _, step in visible:
            marker, style = formatters.plan_marker(str(step.get("status")))
            row = Text(f"{marker} ", style=STYLES[style])
            row.append(elide(str(step.get("goal") or ""), goal_width), style=STYLES[style])
            lines.append(row)
        if hidden_after:
            lines.append(Text(f"… {hidden_after} pending", style=STYLES["muted"]))
        self.render_block(meta, lines)


class ActivityPanel(Panel):
    """What is happening right now: goal, active tool, state, expectation."""

    def __init__(self) -> None:
        super().__init__("active", id="activity")

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        action = ui.action
        meta = ""
        if action is not None and not ui.agent_done:
            elapsed = duration(ui.action_elapsed())
            if action.state == "running":
                meta = f"{action.label} · {elapsed}"
            elif action.state == "waiting":
                # a model call is in flight: show which role is thinking and for how long
                meta = f"{ui.role.lower() or 'model'} · {elapsed}"
            elif action.duration_ms is not None:
                meta = f"{action.label} · {action.duration_ms / 1000:.1f}s"
            else:
                meta = action.label
        lines: list[Text] = []
        if ui.agent_done:
            finished = Text(
                "finished · no further model or tool action", style=STYLES["muted"]
            )
            lines.append(_row("state", finished))
            if action is not None:
                summary = action.label
                if action.detail:
                    summary = f"{summary} · {action.detail}"
                elif action.state in {"done", "failed"}:
                    summary = f"{summary} · {action.state}"
                if action.duration_ms is not None:
                    summary = f"{summary} · {action.duration_ms / 1000:.1f}s"
                lines.append(_row("last", Text(elide(summary, width - LABEL_WIDTH))))
            self.render_block("", lines)
            return
        goal = ui.current_goal or (state.objective if state is not None else "")
        if goal:
            for chunk in _wrap(goal, width - LABEL_WIDTH, 2):
                lines.append(_row("goal", chunk))
        if action is not None and action.state != "waiting":
            label = action.label.upper() if len(action.label) <= 10 else "TOOL"
            for index, argument in enumerate(action.lines[:3]):
                name_label = label if index == 0 else ""
                lines.append(_row(name_label, elide(argument, width - LABEL_WIDTH)))
            state_text = {
                "running": f"RUNNING · {duration(ui.action_elapsed())}",
                "done": (
                    f"done · {action.duration_ms / 1000:.1f}s" if action.duration_ms else "done"
                ),
                "failed": f"FAILED · {action.detail}" if action.detail else "FAILED",
            }.get(action.state, action.state)
            style = {"running": "accent", "done": "success", "failed": "error"}.get(
                action.state, "muted"
            )
            lines.append(_row("state", Text(state_text, style=STYLES[style])))
        elif action is not None:
            detail = action.lines[0] if action.lines else "choosing the next action"
            waiting = Text(
                f"waiting for model · {elide(detail, max(10, width - 26))}",
                style=STYLES["muted"],
            )
            lines.append(_row("state", waiting))
        if action is not None and action.expected:
            for chunk in _wrap(action.expected, width - LABEL_WIDTH, 1):
                lines.append(_row("expect", Text(chunk, style=STYLES["muted"])))
        if not lines:
            lines.append(Text("idle", style=STYLES["muted"]))
        self.render_block(meta, lines)


class CheckpointPanel(Panel):
    """Trusted checkpoint, candidate state and the candidate file scope."""

    def __init__(self) -> None:
        super().__init__("checkpoint", id="checkpoint")
        self.max_files = 4

    def render_state(
        self,
        state: AgentState | None,
        ui: UiState,
        *,
        subject: str,
    ) -> None:
        width = self.content_width
        snapshot = ui.candidate
        dirty = bool(snapshot and snapshot.get("dirty"))
        meta = formatters.snapshot_summary(snapshot) if dirty else ""
        lines: list[Text] = []
        if ui.rollback_active() and ui.rollback:
            discarded = ui.rollback.get("discarded") or []
            detail = (
                f"{len(discarded)} files discarded" if discarded else "candidate state discarded"
            )
            reason = elide(str(ui.rollback.get("reason") or ""), max(10, width - 40))
            lines.append(
                _row(
                    "ROLLBACK",
                    Text(
                        f"{detail} → {short_id(str(ui.rollback.get('to_commit')))}"
                        + (f" · {reason}" if reason else ""),
                        style=f"bold {STYLES['warning']}",
                    ),
                    label_style="warning",
                )
            )
        commit = state.accepted_commit if state is not None else None
        lines.append(
            _row(
                "trusted",
                Text(
                    f"{short_id(commit)}  {elide(subject, max(10, width - 20))}",
                    style=STYLES["success"] if commit else STYLES["muted"],
                ),
            )
        )
        lines.append(
            _row(
                "candidate",
                Text(
                    f"DIRTY · {meta}" if dirty else "CLEAN",
                    style=STYLES["warning"] if dirty else STYLES["success"],
                )
                if snapshot
                else Text("measuring…", style=STYLES["muted"]),
            )
        )
        files = (snapshot or {}).get("files") or []
        for entry in files[: self.max_files]:
            added = entry.get("added")
            deleted = entry.get("deleted")
            delta = ""
            if added is not None or deleted is not None:
                added_text = added if added is not None else "-"
                deleted_text = deleted if deleted is not None else "-"
                delta = f"  +{added_text} -{deleted_text}"
            row = Text("      ")
            row.append(
                Text(f"{formatters.file_label(str(entry.get('code'))):<2} ", style=STYLES["accent"])
            )
            row.append(elide(str(entry.get("path")), max(10, width - 22)))
            row.append(Text(delta, style=STYLES["muted"]))
            lines.append(row)
        if len(files) > self.max_files:
            lines.append(
                Text(
                    f"      … {len(files) - self.max_files} more files (press d)",
                    style=STYLES["muted"],
                )
            )
        self.render_block(meta, lines)


class ValidationPanel(Panel):
    """Deterministic validation, verification criteria and warnings."""

    def __init__(self) -> None:
        super().__init__("validation", id="validation")
        self.max_rows = 6

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        validation = ui.validation
        verification = ui.verification
        meta = ""
        lines: list[Text] = []
        if validation is None and verification is None:
            lines.append(Text("not run yet", style=STYLES["muted"]))
        if validation is not None:
            commands = validation.get("commands") or []
            results = validation.get("command_results") or []
            meta = "PASS" if validation.get("passed") else "FAIL"
            for index, result in enumerate(results[: self.max_rows]):
                success = bool(result.get("success"))
                label = str(commands[index]) if index < len(commands) else str(result.get("tool"))
                row = Text("")
                row.append_text(
                    Text("✓ " if success else "× ", style=STYLES["success" if success else "error"])
                )
                row.append(elide(label, max(12, width - 22)), style=STYLES["value"])
                detail = "exit 0" if success else f"exit {result.get('exit_code')}"
                if result.get("duration_ms") is not None:
                    detail += f" · {result['duration_ms'] / 1000:.1f}s"
                row.append_text(
                    formatters.pad_row(
                        Text(""), Text(detail, style=STYLES["muted"]), width - len(row.plain)
                    )
                )
                lines.append(row)
            if "diff_check_passed" in validation:
                ok = bool(validation.get("diff_check_passed"))
                row = Text("")
                row.append_text(
                    Text("✓ " if ok else "× ", style=STYLES["success" if ok else "error"])
                )
                row.append("git diff --check", style=STYLES["value"])
                lines.append(row)
            if not results and not validation.get("diff_check_passed", True):
                lines.append(
                    _row("scope", Text("diff check failed", style=STYLES["error"]))
                )
            for warning in (validation.get("warnings") or [])[:2]:
                warn_text = Text(
                    elide(str(warning), width - LABEL_WIDTH), style=STYLES["warning"]
                )
                lines.append(_row("warn", warn_text))
        if verification is not None:
            criteria = verification.get("criteria") or []
            room = max(0, self.max_rows - len(lines))
            for item in criteria[:room]:
                ok = bool(item.get("passed"))
                row = Text("")
                row.append_text(
                    Text("✓ " if ok else "× ", style=STYLES["success" if ok else "error"])
                )
                criterion = elide(str(item.get("criterion")), max(12, width - 4))
                row.append(criterion, style=STYLES["value"])
                lines.append(row)
                if not ok and item.get("evidence"):
                    evidence = elide(str(item["evidence"]), width - 6)
                    lines.append(Text("    " + evidence, style=STYLES["muted"]))
            meta = "VERIFIED" if verification.get("passed") else "FAILED"
        if ui.evaluation is not None:
            decision = str(ui.evaluation.get("decision"))
            reason = elide(str(ui.evaluation.get("reason") or ""), max(10, width - LABEL_WIDTH))
            style = {
                "accept": "success",
                "rollback": "warning",
                "replan": "warning",
                "continue": "muted",
                "finish_candidate": "accent",
            }.get(decision, "muted")
            lines.append(
                _row("decision", Text(f"{decision} · {reason}", style=STYLES[style]))
            )
        self.render_block(meta, lines)


class MetricsPanel(Panel):
    """One compact strip: context usage, memory, iteration."""

    def __init__(self) -> None:
        super().__init__("", id="metrics")

    def render_state(self, state: AgentState | None, ui: UiState, *, limit: int) -> None:
        width = self.content_width
        row = Text()
        bar, label = formatters.context_usage(ui.context, limit)
        row.append("CTX ", style=STYLES["title"])
        if bar:
            row.append(bar, style=STYLES["accent"])
            row.append(" ")
        row.append(label, style=STYLES["value"])
        if width >= 100:
            row.append("   MEM ", style=STYLES["title"])
            row.append(
                formatters.memory_summary(ui.memory, compact=width < 120), style=STYLES["value"]
            )
        if state is not None and width >= 90:
            row.append("   ITER ", style=STYLES["title"])
            row.append(str(state.iteration), style=STYLES["value"])
        if state is not None and state.pending_question and width >= 110:
            row.append("   ? ", style=STYLES["title"])
            row.append(elide(state.pending_question, 40), style=STYLES["warning"])
        self.set_body(row)


class TimelinePanel(Panel):
    """Curated meaningful events, newest last."""

    def __init__(self) -> None:
        super().__init__("events", id="timeline")
        self.rows = 8

    def render_state(self, ui: UiState) -> None:
        width = self.content_width
        entries = ui.timeline[-max(1, self.rows) :]
        hidden = len(ui.timeline) - len(entries)
        lines: list[Text] = []
        if hidden > 0:
            lines.append(Text(f"… {hidden} earlier events (l for logs)", style=STYLES["muted"]))
        for entry in entries:
            row = Text(entry.at.astimezone().strftime("%H:%M:%S") + " ", style=STYLES["muted"])
            row.append(f"{entry.icon} ", style=STYLES[entry.style])
            row.append(elide(entry.text, max(10, width - 14)), style=STYLES["value"])
            lines.append(row)
        if not entries:
            lines.append(Text("no events yet", style=STYLES["muted"]))
        self.render_block("", lines)


class FooterBar(Static):
    """One-line shortcut bar. Hints change with the run state."""

    can_focus = False

    def __init__(self) -> None:
        super().__init__("", id="footer", markup=False)
        self.body = Text("")

    def render_text(self, text: str) -> None:
        self.body = Text(text, style=STYLES["muted"])
        self.update(self.body)
