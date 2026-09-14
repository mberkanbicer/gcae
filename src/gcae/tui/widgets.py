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

    def row_budget(self, cap: int) -> int:
        """How many body rows to draw: the space the layout gave this panel, up to ``cap``.

        Panels that grow (``height: 1fr``) use this so slack becomes *more content*
        instead of a blank band at the bottom of a box.
        """
        height = self.size.height or 0
        if height <= 2:
            return cap
        return max(2, min(cap, height - 1))

    def set_body(self, body: Text) -> None:
        """Render Rich text and keep a readable copy for tests and the demo."""
        self.body = body
        self.update(body)

    def render_block(self, meta: str, lines: list[Text], *, meta_style: str = "") -> None:
        width = self.content_width
        body = Text()
        if self.panel_title:
            header = Text(self.panel_title.upper(), style=STYLES["title"])
            if meta:
                style = meta_style or STYLES["muted"]
                header = formatters.pad_row(header, Text(meta, style=style), width)
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
        degraded: bool = False,
    ) -> None:
        width = self.content_width
        status = state.status if state is not None else "starting"
        phase = state.phase.value if state is not None else None
        badge, badge_style = formatters.run_state(status, phase, paused)
        row = Text("GCAE", style="bold")
        row.append(" │ ", style=STYLES["muted"])
        row.append(badge, style=f"bold {STYLES[badge_style]}")
        active = state is None or not str(status).startswith(("complete", "failed", "stopped"))
        if ui.rollback_active() and active:
            row.append(" │ ROLLBACK", style=f"bold {STYLES['warning']}")
        if paused:
            row.append(" │ paused · no new model or tool action", style=STYLES["warning"])
        if degraded and ui.degradations:
            row.append(
                f" │ DEGRADED · {elide(ui.degradations[-1], 40)}",
                style=f"bold {STYLES['warning']}",
            )
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
            if width >= 74:
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
            if state is not None and state.merge is not None:
                failure_merge = state.merge
                merged_text = Text(
                    f"merged into {failure_merge.target_branch} · "
                    f"{short_id(failure_merge.merge_commit)}"
                    " (accepted work, final verification did not pass) · undo: "
                    f"gcae undo <repo> {state.run_id}",
                    style=STYLES["success"],
                )
                lines.append(_row("merged", merged_text))
            elif state is not None and state.accepted_steps:
                rescue = Text(
                    f"{state.accepted_steps} accepted step(s) are on branch {state.branch} · "
                    "press M (or gcae merge) to bring them into your working tree",
                    style=STYLES["accent"],
                )
                lines.append(_row("recover", rescue))
                lines.extend(_work_location_rows(state, ui))
        elif state is not None and state.status == "blocked":
            title = Text("RUN BLOCKED", style=f"bold {STYLES['warning']}")
            for chunk in _wrap(
                state.blocked_reason or "no safe autonomous path remains",
                width - LABEL_WIDTH,
                3,
            ):
                lines.append(_row("blocked", chunk))
            if state.unblock_hint:
                for chunk in _wrap(f"unblocks with: {state.unblock_hint}", width - LABEL_WIDTH, 2):
                    lines.append(_row("unblock", Text(chunk, style=STYLES["accent"])))
            if state.accepted_commit:
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
            lines.append(
                _row("next", Text("press i to send input or instructions", style=STYLES["muted"]))
            )
        elif ui.pending_input is not None:
            title = Text("INPUT REQUIRED", style=f"bold {STYLES['warning']}")
            prompt = str(ui.pending_input.get("prompt") or "")
            for chunk in _wrap(prompt, width - LABEL_WIDTH, 2):
                lines.append(_row("prompt", chunk))
            lines.append(
                _row(
                    "process",
                    elide(str(ui.pending_input.get("command") or ""), width - LABEL_WIDTH),
                )
            )
            lines.append(
                _row(
                    "next",
                    Text(
                        "press i to send the answer (the process is still alive)",
                        style=STYLES["muted"],
                    ),
                )
            )
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
            lines.extend(_work_location_rows(state, ui))
            merge = state.merge
            if merge is not None:
                verified = "" if state.status == "complete" else " (accepted work, unverified)"
                merge_text = Text(
                    f"merged into {merge.target_branch} · {short_id(merge.merge_commit)}"
                    f"{verified} · undo: gcae undo <repo> {state.run_id}",
                    style=STYLES["success"],
                )
            else:
                merge_text = Text(
                    "not merged · press M to merge into the current branch, or run "
                    f"`gcae merge <repo> {state.run_id}`",
                    style=STYLES["warning"],
                )
            lines.append(_row("merge", merge_text))
        elif state is not None and state.status == "stopped":
            title = Text("RUN STOPPED", style=f"bold {STYLES['muted']}")
            stopped_merge = state.merge
            if stopped_merge is not None:
                lines.append(
                    _row(
                        "trusted",
                        f"{short_id(state.accepted_commit)} on {state.branch} · merged into "
                        f"{stopped_merge.target_branch} ({short_id(stopped_merge.merge_commit)})",
                    )
                )
            else:
                lines.append(
                    _row("trusted", f"{short_id(state.accepted_commit)} on {state.branch}")
                )
            if state.accepted_steps:
                rescue = Text(
                    f"{state.accepted_steps} accepted step(s) · press M to merge them",
                    style=STYLES["accent"],
                )
                lines.append(_row("recover", rescue))
        if not title.plain:
            self.set_body(Text(""))
            return False
        body = formatters.pad_row(title, Text(""), width)
        for line in lines:
            body.append("\n")
            body.append_text(line)
        self.set_body(body)
        return True


def _work_location_rows(state: AgentState, ui: UiState) -> list[Text]:
    """Tell the user exactly which folder holds the generated documents right now."""
    files = ui.work_files
    if not files:
        return []
    preview = ", ".join(elide(name, 34) for name in files[:3])
    if len(files) > 3:
        preview += f" (+{len(files) - 3} more)"
    rows = [_row("files", Text(preview))]
    if state.merge is not None:
        rows.append(_row("on disk", Text(state.source_repo, style=STYLES["success"])))
    else:
        rows.append(
            _row(
                "on disk",
                Text(
                    f"{state.worktree} (worktree, not merged yet)", style=STYLES["warning"]
                ),
            )
        )
    return rows


class ObjectivePanel(Panel):
    """Original objective and the goal being worked on now."""

    def __init__(self) -> None:
        super().__init__("objective", id="objective")

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        objective = state.objective if state is not None else (ui.request or "")
        goal = ui.current_goal or (ui.plan[0].get("goal", "") if ui.plan else "")
        if ui.agent_done and not goal:
            goal = ui.last_goal
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
                _row("request" if index == 0 else "", Text(chunk, style=STYLES["muted"]))
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
        goal_width = max(10, width - 4)
        # A long active goal is the one thing the user must be able to read, so it wraps onto
        # continuation rows and the neighbouring steps yield to it instead of being pushed out.
        active_rows = _wrap(str(steps[active].get("goal") or ""), goal_width, 3)
        rows = self.row_budget(self.max_rows)
        neighbours = max(1, rows - len(active_rows) + 1)
        visible, hidden_before, hidden_after = _window(steps, active, neighbours)
        lines: list[Text] = []
        if hidden_before:
            lines.append(Text(f"… {hidden_before} completed", style=STYLES["muted"]))
        for index, step in visible:
            marker, style = formatters.plan_marker(str(step.get("status")))
            goal = str(step.get("goal") or "")
            wrapped = active_rows if index == active else _wrap(goal, goal_width, 1)
            for row_index, chunk in enumerate(wrapped):
                row = Text(f"{marker} " if row_index == 0 else "  ", style=STYLES[style])
                row.append(chunk, style=STYLES[style])
                lines.append(row)
        if hidden_after:
            lines.append(Text(f"… {hidden_after} pending", style=STYLES["muted"]))
        self.render_block(meta, lines)


class ActivityPanel(Panel):
    """What is happening right now: goal, action, target, expectation, state.

    Deliberately free of model telemetry.  Streaming characters, first-token latency and
    partial generations are debug detail: they live in the log screen, and at most a
    single one-line state here ("controller · generating · 3.4s").
    """

    #: cap for the fixed box; the layout normally decides through ``row_budget``
    max_rows: int = 6

    def __init__(self) -> None:
        super().__init__("active", id="activity")

    def render_state(self, state: AgentState | None, ui: UiState, *, model: str = "") -> None:
        """Rows in priority order, truncated to the fixed box: nothing below ever moves.

        Order (most important first): goal, action, model state, target, expected, state,
        why.  The box height is constant, so a model that starts streaming cannot reflow the
        column; only content that genuinely arrived changes what is shown.
        """
        width = self.content_width
        action = ui.action
        rows: list[Text] = []
        budget = self.row_budget(self.max_rows)

        if ui.agent_done:
            rows.append(
                _row(
                    "state",
                    Text("finished · no further model or tool action", style=STYLES["muted"]),
                )
            )
            if action is not None:
                summary = action.label if not action.detail else f"{action.label} · {action.detail}"
                rows.append(_row("last", Text(elide(summary, width - LABEL_WIDTH))))
            self.render_block("", rows)
            return

        goal = ui.current_goal or (state.objective if state is not None else "")
        if goal:
            for chunk in _wrap(goal, width - LABEL_WIDTH, 2):
                rows.append(_row("goal", Text(chunk, style=STYLES["current"])))
        else:
            rows.append(_row("goal", Text("waiting for the first plan", style=STYLES["muted"])))

        if ui.pending_input is not None:
            # a live process is waiting for the user: say so instead of showing a timeout
            prompt = str(ui.pending_input.get("prompt") or "input required")
            rows.append(
                Text("INPUT REQUIRED", style=f"bold {STYLES['warning']}")
            )
            rows.append(
                _row(
                    "process",
                    Text(
                        elide(str(ui.pending_input.get("command") or ""), width - LABEL_WIDTH)
                    ),
                )
            )
            for chunk in _wrap(f"the process is waiting for: {prompt}", width - LABEL_WIDTH, 2):
                rows.append(_row("prompt", Text(chunk, style=STYLES["warning"])))
            rows.append(
                _row(
                    "state",
                    Text(
                        "WAITING FOR USER · press i to send the answer",
                        style=STYLES["accent"],
                    ),
                )
            )
            self.render_block("", rows[:budget])
            return

        if action is not None and action.kind == "model":
            role = action.label or ui.provider_role or "model"
            what = "waiting" if action.state == "waiting" and not ui.stream else "generating"
            rows.append(
                _row(
                    "model",
                    Text(
                        elide(
                            f"{role} · {what} · {duration(ui.action_elapsed())}",
                            width - LABEL_WIDTH,
                        ),
                        style=STYLES["accent"],
                    ),
                )
            )
            if model:
                rows.append(
                    _row("", Text(elide(model, width - LABEL_WIDTH), style=STYLES["muted"]))
                )
            if action.expected:
                rows.append(
                    _row(
                        "expected",
                        Text(elide(action.expected, width - LABEL_WIDTH), style=STYLES["muted"]),
                    )
                )
            if action.lines:
                rows.append(
                    _row(
                        "why",
                        Text(elide(action.lines[0], width - LABEL_WIDTH), style=STYLES["muted"]),
                    )
                )
        elif action is not None and action.state != "waiting":
            rows.append(_row("action", Text(elide(action.label, width - LABEL_WIDTH))))
            if ui.execution_mode:
                mode_detail = ui.execution_mode.replace("_", " ").upper()
                if ui.stdin_lines:
                    mode_detail += f" · {ui.stdin_lines} answers queued"
                elif ui.interactive:
                    mode_detail += " · may ask for input"
                rows.append(
                    _row(
                        "mode",
                        Text(elide(mode_detail, width - LABEL_WIDTH), style=STYLES["accent"]),
                    )
                )
            if action.target:
                rows.append(
                    _row(
                        "target",
                        Text(elide(action.target, width - LABEL_WIDTH), style=STYLES["value"]),
                    )
                )
            if action.expected:
                rows.append(
                    _row(
                        "expected",
                        Text(elide(action.expected, width - LABEL_WIDTH), style=STYLES["muted"]),
                    )
                )
            state_text, style = {
                "running": (f"RUNNING · {duration(ui.action_elapsed())}", "accent"),
                "done": (
                    f"DONE · {action.duration_ms / 1000:.1f}s" if action.duration_ms else "DONE",
                    "success",
                ),
                "failed": (f"FAILED · {action.detail}" if action.detail else "FAILED", "error"),
                "recovered": ("RECOVERED · the runtime corrected its own approach", "accent"),
            }.get(action.state, (action.state.upper(), "muted"))
            rows.append(_row("state", Text(state_text, style=STYLES[style])))
            detail = action.detail or (action.lines[0] if action.lines else "")
            if detail:
                rows.append(
                    _row("why", Text(elide(detail, width - LABEL_WIDTH), style=STYLES["muted"]))
                )
        elif action is not None:
            rows.append(_row("action", Text(elide(action.label, width - LABEL_WIDTH))))
            for line in action.lines[:2]:
                rows.append(
                    _row("", Text(elide(line, width - LABEL_WIDTH), style=STYLES["muted"]))
                )
            rows.append(
                _row(
                    "state",
                    Text(f"WAITING · {duration(ui.action_elapsed())}", style=STYLES["accent"]),
                )
            )
        else:
            rows.append(_row("state", Text("idle · waiting for the agent", style=STYLES["muted"])))

        self.render_block("", rows[:budget])


class CheckpointPanel(Panel):
    """Trusted checkpoint versus speculative candidate — GCAE's defining distinction."""

    #: The dashboard shows the top few files and counts the rest; the diff screen lists them
    #: all.  A short list means the panel grows once (when speculative work first appears)
    #: instead of growing every time the agent touches another file.
    max_files: int = 2

    def __init__(self) -> None:
        super().__init__("checkpoint", id="checkpoint")

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
        lines: list[Text] = []
        if ui.rollback_active() and ui.rollback:
            discarded = ui.rollback.get("discarded") or []
            detail = f"{len(discarded)} files discarded" if discarded else "candidate discarded"
            reason = elide(str(ui.rollback.get("reason") or ""), max(10, width - 34))
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
                "TRUSTED",
                Text(
                    f"{short_id(commit)}  {elide(subject, max(10, width - 26))}",
                    style=STYLES["success"] if commit else STYLES["muted"],
                ),
            )
        )
        if snapshot is None:
            lines.append(_row("CANDIDATE", Text("measuring…", style=STYLES["muted"])))
        elif dirty:
            lines.append(
                _row(
                    "CANDIDATE",
                    Text(
                        f"DIRTY · {formatters.snapshot_summary(snapshot)}",
                        style=f"bold {STYLES['warning']}",
                    ),
                )
            )
            for entry in (snapshot.get("files") or [])[: self.max_files]:
                added = entry.get("added")
                deleted = entry.get("deleted")
                delta = ""
                if added is not None or deleted is not None:
                    plus = added if added is not None else "-"
                    minus = deleted if deleted is not None else "-"
                    delta = f"  +{plus} -{minus}"
                row = Text("          ")
                row.append(
                    Text(
                        f"{formatters.file_label(str(entry.get('code')))} ",
                        style=STYLES["warning"],
                    )
                )
                row.append(elide(str(entry.get("path")), max(10, width - 26)))
                row.append(Text(delta, style=STYLES["muted"]))
                lines.append(row)
            extra = len(snapshot.get("files") or []) - self.max_files
            if extra > 0:
                lines.append(
                    Text(
                        f"          + {extra} more files · press d for the diff",
                        style=STYLES["muted"],
                    )
                )
        else:
            lines.append(
                _row(
                    "CANDIDATE",
                    Text("CLEAN · no speculative changes", style=STYLES["success"]),
                )
            )
        if ui.rollback is not None and not ui.rollback_active():
            lines.append(
                _row(
                    "restored",
                    Text(
                        "after rejection · "
                        f"{short_id(str(ui.rollback.get('to_commit')))} is trusted",
                        style=STYLES["muted"],
                    ),
                )
            )
        self.render_block("", lines)


class ValidationPanel(Panel):
    """Deterministic validation and the final verification gate.

    Structured states, never raw output: ``✓`` pass, ``×`` fail, ``…`` running,
    ``–`` skipped.  The evaluator's decision lives in its own panel.
    """

    def __init__(self) -> None:
        super().__init__("validation", id="validation")
        self.max_rows = 6

    def render_state(self, state: AgentState | None, ui: UiState) -> None:
        width = self.content_width
        validation = ui.validation
        verification = ui.verification
        # (priority, row): 0 failure, 1 gate/warning, 2 pass, 3 note
        rows: list[tuple[int, Text]] = []
        running = not ui.agent_done and ui.phase == "validate"
        if validation is None and verification is None:
            message = (
                "… validating the candidate" if running else "– awaiting a candidate to validate"
            )
            rows.append((1, Text(message, style=STYLES["muted"])))
        if validation is not None:
            commands = validation.get("commands") or []
            results = validation.get("command_results") or []
            for index, result in enumerate(results):
                success = bool(result.get("success"))
                label = str(commands[index]) if index < len(commands) else str(result.get("tool"))
                rows.append((2 if success else 0, self._check_row(success, label, result, width)))
            if validation.get("diff_check_passed") is not None:
                ok = bool(validation.get("diff_check_passed"))
                rows.append(
                    (
                        2 if ok else 0,
                        self._check_row(
                            ok,
                            "git diff --check",
                            {"evidence": "" if ok else "conflict markers or whitespace errors"},
                            width,
                        ),
                    )
                )
            for path in (validation.get("scope_violations") or [])[:2]:
                rows.append(
                    (
                        1,
                        _row(
                            "scope",
                            Text(
                                elide(f"outside the intended scope: {path}", width - LABEL_WIDTH),
                                style=STYLES["warning"],
                            ),
                        ),
                    )
                )
            for warning in (validation.get("warnings") or [])[:2]:
                rows.append(
                    (
                        3,
                        _row(
                            "note",
                            Text(elide(str(warning), width - LABEL_WIDTH), style=STYLES["muted"]),
                        ),
                    )
                )
        if verification is not None:
            passed = bool(verification.get("passed"))
            rows.append(
                (
                    1 if passed else 0,
                    _row(
                        "criteria",
                        Text(
                            "final verification" if passed else "verification failed",
                            style=STYLES["success"] if passed else STYLES["error"],
                        ),
                    ),
                )
            )
            for item in verification.get("criteria") or []:
                ok = bool(item.get("passed"))
                rows.append(
                    (1 if ok else 0, self._check_row(ok, str(item.get("criterion")), item, width))
                )
        # Failures first, then the gate, then passing checks and notes: a short panel must
        # never hide the check that actually failed.
        budget = self.row_budget(self.max_rows)
        ordered = sorted(range(len(rows)), key=lambda index: (rows[index][0], index))
        lines = [rows[index][1] for index in ordered[:budget]]
        if len(rows) > budget:
            lines.append(
                Text(f"… {len(rows) - budget} more checks · l for the log", style=STYLES["muted"])
            )
        self.render_block("", lines)

    @staticmethod
    def _check_row(
        success: bool, label: str, result: dict[str, Any], width: int
    ) -> Text:
        """One check: marker, label, and the shortest honest detail available."""
        row = Text("")
        row.append_text(
            Text("✓ " if success else "× ", style=STYLES["success" if success else "error"])
        )
        row.append(elide(label, max(12, width - 24)), style=STYLES["value"])
        detail = ""
        if success:
            if result.get("duration_ms") is not None:
                detail = f"{result['duration_ms'] / 1000:.1f}s"
        else:
            error = str(result.get("error") or result.get("evidence") or "")
            lines = error.strip().splitlines()
            if lines:
                detail = elide(lines[0], 40)
            elif result.get("exit_code") is not None:
                detail = f"exit {result['exit_code']}"
        if detail:
            row.append_text(
                formatters.pad_row(
                    Text(""), Text(detail, style=STYLES["muted"]), width - len(row.plain)
                )
            )
        return row


class EvaluationPanel(Panel):
    """The evaluator's latest decision: the gate every candidate must pass."""

    DECISIONS: dict[str, tuple[str, str, str]] = {
        "accept": ("ACCEPTED", "success", "the candidate advanced the objective"),
        "rollback": ("ROLLBACK", "warning", "the candidate was rejected and discarded"),
        "replan": ("REPLAN", "warning", "the plan needed to change"),
        "continue": ("CONTINUE", "muted", "more work is needed before a checkpoint"),
        "finish_candidate": ("FINISH CANDIDATE", "accent", "the planned work is complete"),
    }

    #: cap used when the layout has not sized the panel yet
    max_rows: int = 4

    def __init__(self) -> None:
        super().__init__("evaluation", id="evaluation")

    def render_state(self, state: AgentState | None, ui: UiState, *, subject: str = "") -> None:
        width = self.content_width
        evaluation = ui.evaluation
        lines: list[Text] = []
        if evaluation is None:
            message = "waiting for the first evaluation" if not ui.agent_done else "no evaluation"
            lines.append(Text(message, style=STYLES["muted"]))
            self.render_block("", lines)
            return
        decision = str(evaluation.get("decision") or "")
        word, style, meaning = self.DECISIONS.get(
            decision, (decision.upper() or "UNKNOWN", "muted", "")
        )
        meta = word
        reason = " ".join(str(evaluation.get("reason") or "").split())
        rows = max(1, self.row_budget(self.max_rows) - 1)
        for chunk in _wrap(reason or meaning, width - 1, rows):
            lines.append(Text(chunk, style=STYLES["value"]))
        if decision == "rollback":
            restored = short_id(str(ui.rollback.get("to_commit"))) if ui.rollback else ""
            suffix = f"restored {restored}" if restored else "candidate discarded"
            lines.append(_row("state", Text(suffix, style=STYLES["muted"])))
        elif decision == "accept" and subject:
            lines.append(
                _row("step", Text(elide(subject, width - LABEL_WIDTH), style=STYLES["muted"]))
            )
        elif decision == "finish_candidate":
            lines.append(
                _row("next", Text("entering final verification", style=STYLES["muted"]))
            )
        self.render_block(meta, lines, meta_style=f"bold {STYLES[style]}")


class MetricsPanel(Panel):
    """One compact strip: context usage, memory, iteration."""

    def __init__(self) -> None:
        super().__init__("", id="metrics")

    def render_state(self, state: AgentState | None, ui: UiState, *, limit: int) -> None:
        """One compact strip: context, memory, iterations.  No invented numbers."""
        width = self.content_width
        row = Text()
        bar_width = 10 if width >= 110 else 0
        bar, label = formatters.context_usage(ui.context, limit, bar_width=bar_width or 10)
        row.append("CTX ", style=STYLES["title"])
        if bar and bar_width:
            row.append(bar, style=STYLES["accent"])
            row.append(" ")
        shown = label.replace("(est)", "est") if bar_width else label
        row.append(shown, style=STYLES["value"])
        counts = ui.memory
        if width >= 78:
            row.append("   MEM ", style=STYLES["title"])
            if width >= 118:
                row.append(formatters.memory_summary(counts), style=STYLES["value"])
            else:
                row.append(formatters.memory_summary(counts, compact=True), style=STYLES["value"])
                failures = counts.get("failure", 0)
                row.append("   FAIL ", style=STYLES["title"])
                row.append(
                    str(failures),
                    style=STYLES["error"] if failures else STYLES["value"],
                )
        if state is not None:
            row.append("   ITER ", style=STYLES["title"])
            row.append(str(state.iteration), style=STYLES["value"])
        if state is not None and state.pending_question and width >= 110:
            row.append("   ? ", style=STYLES["title"])
            row.append(elide(state.pending_question, 40), style=STYLES["warning"])
        self.set_body(row)


class TimelinePanel(Panel):
    """The semantic story of the run, newest last.

    Only events that changed the run's state appear here — never model telemetry.  The
    log screen keeps the complete record.
    """

    def __init__(self) -> None:
        super().__init__("events", id="timeline")
        self.rows = 8

    def render_state(self, ui: UiState) -> None:
        width = self.content_width
        rows = max(1, self.rows)
        entries = ui.timeline[-rows:]
        hidden = len(ui.timeline) - len(entries)
        lines: list[Text] = []
        if hidden > 0:
            lines.append(Text(f"… {hidden} earlier · l for the full log", style=STYLES["muted"]))
        for entry in entries:
            row = Text(entry.at.astimezone().strftime("%H:%M:%S") + " ", style=STYLES["muted"])
            row.append(f"{entry.icon} ", style=STYLES[entry.style])
            row.append(elide(entry.text, max(10, width - 14)), style=STYLES["value"])
            lines.append(row)
        if not entries:
            lines.append(Text("waiting for the first semantic step", style=STYLES["muted"]))
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
