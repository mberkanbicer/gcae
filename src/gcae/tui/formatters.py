"""Presentation helpers for the GCAE dashboard.

Pure functions only: no Textual imports, no runtime imports, no I/O.  Every value
rendered here comes from a runtime event payload or from ``AgentState``; nothing is
invented or estimated except where the text says so.

Semantic style keys map to one palette in :data:`STYLES`, which is the single source
of truth for text colour.  Widget-level styling lives in ``styles.tcss``.
"""

from __future__ import annotations

from typing import Any

from rich.text import Text

STYLES: dict[str, str] = {
    "title": "dim bold",
    "label": "dim",
    "muted": "dim",
    "value": "",
    "accent": "bold cyan",
    "success": "green",
    "warning": "yellow",
    "error": "red",
    "current": "bold cyan",
    "done": "dim green",
    "pending": "dim",
    "rule": "dim",
}

PHASE_ROLE: dict[str, str] = {
    "analyze": "PLAN",
    "plan": "PLAN",
    "execute": "ACT",
    "validate": "EVAL",
    "evaluate": "EVAL",
    "checkpoint": "EVAL",
    "rollback": "PLAN",
    "verify": "VERIFY",
}

PLAN_MARKERS: dict[str, tuple[str, str]] = {
    "completed": ("✓", "done"),
    "active": ("●", "current"),
    "pending": ("○", "pending"),
    "failed": ("×", "error"),
    "skipped": ("–", "muted"),
}

RUN_BADGES: dict[str, tuple[str, str]] = {
    "running": ("RUNNING", "accent"),
    "waiting_for_user": ("WAITING", "warning"),
    "complete": ("COMPLETE", "success"),
    "stopped": ("STOPPED", "muted"),
}

PORCELAIN_LABELS: dict[str, str] = {
    " M": "M",
    "M ": "M",
    "MM": "M",
    "??": "A",
    "A ": "A",
    "AM": "A",
    " D": "D",
    "D ": "D",
    "R ": "R",
    "RM": "R",
    " C": "C",
    "T ": "T",
    "UU": "U",
}

MEMORY_KINDS: tuple[tuple[str, str], ...] = (
    ("user_instruction", "instructions"),
    ("fact", "facts"),
    ("decision", "decisions"),
    ("failure", "failed paths"),
    ("observation", "observations"),
    ("artifact", "artifacts"),
)


def status_style(status: str) -> str:
    """Style key for a run status string (``failed: reason`` maps to ``failed``)."""
    head = status.split(":", 1)[0].strip().lower()
    if head == "failed":
        return "error"
    if head == "complete":
        return "success"
    if head == "stopped":
        return "muted"
    return "accent"



def role_for_phase(phase: str) -> str:
    return PHASE_ROLE.get(phase, "ACT")

#: What the agent is doing, as a word the user can act on.  Derived from the real phase,
#: never invented: a run that is not running keeps its own terminal status.
PHASE_STATE: dict[str, str] = {
    "analyze": "PLANNING",
    "plan": "PLANNING",
    "execute": "ACTING",
    "validate": "VALIDATING",
    "evaluate": "EVALUATING",
    "checkpoint": "CHECKPOINTING",
    "rollback": "ROLLING BACK",
    "verify": "VERIFYING",
    "complete": "COMPLETE",
    "failed": "FAILED",
}


def run_state(status: str, phase: str | None, paused: bool) -> tuple[str, str]:
    """(label, style key) for the top status bar."""
    head = status.split(":", 1)[0].strip().lower()
    if head in {"complete", "failed", "stopped", "waiting_for_user"}:
        return RUN_BADGES.get(head, (head.upper(), status_style(status)))
    if paused:
        return ("PAUSED", "warning")
    if phase:
        label = PHASE_STATE.get(phase)
        if label:
            return (label, "accent")
    return RUN_BADGES.get(head, (head.upper() or "UNKNOWN", status_style(status)))


def short_id(value: str | None, length: int = 7) -> str:
    if not value:
        return "-"
    return value[:length]


def duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    if seconds < 1:
        return f"{seconds:.1f}s"
    if seconds < 10:
        return f"{seconds:.1f}s"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def human_tokens(count: int) -> str:
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"



def elide(text: str, width: int) -> str:
    if width <= 1:
        return text[:1]
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def pad_row(left: Text, right: Text, width: int) -> Text:
    """One line: left content, right content pushed to the edge."""
    left_length = left.cell_len
    right_length = right.cell_len
    gap = max(1, width - left_length - right_length)
    row = left.copy()
    if left_length + right_length + gap > width:
        right = Text(elide(right.plain, max(0, width - left_length - 1)))
        right_length = right.cell_len
        gap = max(1, width - left_length - right_length)
    row.append(" " * gap)
    row.append_text(right)
    return row


def plan_marker(status: str) -> tuple[str, str]:
    return PLAN_MARKERS.get(status, ("○", "pending"))


def file_label(code: str) -> str:
    return PORCELAIN_LABELS.get(code, code.strip() or "?")


def snapshot_summary(snapshot: dict[str, Any] | None) -> str:
    if not snapshot or not snapshot.get("dirty"):
        return "CLEAN"
    files = snapshot.get("files") or []
    added = snapshot.get("added") or 0
    deleted = snapshot.get("deleted") or 0
    return f"{len(files)} file{'s' if len(files) != 1 else ''} · +{added} -{deleted}"


def context_usage(info: dict[str, Any], limit: int, bar_width: int = 10) -> tuple[str, str]:
    used = int(info.get("estimated_tokens") or 0)
    if limit <= 0:
        return ("", f"{human_tokens(used)} tok (est)")
    ratio = min(1.0, used / limit)
    filled = int(round(ratio * bar_width))
    bar = "█" * filled + "░" * (bar_width - filled)
    return (bar, f"{human_tokens(used)}/{human_tokens(limit)} · {int(ratio * 100)}% (est)")


def memory_summary(counts: dict[str, int], compact: bool = False) -> str:
    if not counts:
        return "empty"
    parts: list[str] = []
    for kind, label in MEMORY_KINDS:
        total = counts.get(kind, 0)
        if not total:
            continue
        parts.append(f"{total} {label}")
    for kind, total in sorted(counts.items()):
        if kind not in {name for name, _ in MEMORY_KINDS} and total:
            parts.append(f"{total} {kind}")
    if not parts:
        return "empty"
    if compact:
        return " · ".join(parts[:2])
    return " · ".join(parts)


def _argument(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def tool_presentation(name: str, arguments: dict[str, Any] | None) -> tuple[str, str]:
    """(action, target) in human words — no raw JSON, no tool names on screen.

    ``action`` is a verb phrase; ``target`` is the path, scope or command it acts on.
    Unknown tools fall back to their name so a new tool is visible, never blank.
    """
    args = arguments or {}
    if name in {"write_file", "create_file"}:
        verb = "Create" if name == "create_file" else "Write"
        target = _argument(args, "path")
        return (f"{verb} {target}" if target else verb, target)
    if name == "read_file":
        target = _argument(args, "path")
        return (f"Read {target}" if target else "Read file", target)
    if name == "apply_patch":
        return ("Apply a patch", "")
    if name == "search_text":
        query = _argument(args, "query")
        scope = _argument(args, "path")
        action = f'Search "{query}"' if query else "Search text"
        return (action, scope)
    if name == "list_files":
        scope = _argument(args, "path") or _argument(args, "pattern")
        return ("List files", scope)
    if name == "run_tests":
        return ("Run the project tests", "")
    if name == "run_command":
        command = _argument(args, "command")
        return (command or "Run a command", "")
    return (name.replace("_", " ").capitalize(), _argument(args, "path"))


def timeline_entry(
    event_type: str, payload: dict[str, Any], succeeded: bool | None = None
) -> tuple[str, str, str] | None:
    """Curated timeline line for an event: (icon, text, style key) or None.

    Routine tool successes are intentionally dropped; the log screen keeps them.
    """
    if event_type == "run_started":
        return ("●", f"run started · {elide(str(payload.get('objective', '')), 80)}", "accent")
    if event_type == "run_resumed":
        return ("●", "run resumed", "accent")
    if event_type == "planner_fallback":
        reason = elide(str(payload.get("reason") or ""), 70)
        return ("!", f"planner unavailable · single-step plan · {reason}", "warning")
    if event_type == "repository_notice":
        if payload.get("kind") in {"base", "empty"}:
            file_count = int(payload.get("files") or 0)
            detail = f"{file_count} files" if file_count else "empty repository"
            commit = short_id(str(payload.get("commit")))
            return ("+", f"base commit created · {detail} · {commit}", "success")
        return ("!", str(payload.get("message") or "repository notice"), "warning")
    if event_type == "plan_updated":
        reason = str(payload.get("reason") or "plan updated")
        if reason == "initial plan":
            return ("●", f"plan created · {len(payload.get('steps') or [])} steps", "accent")
        if payload.get("replaced"):
            outcome = "failed" if payload.get("failed") else "replaced"
            return ("↻", f"plan updated · {payload['replaced']} {outcome} · {reason}", "warning")
        return ("↻", f"plan updated · {reason}", "warning")
    if event_type == "step_started":
        index = payload.get("index")
        total = payload.get("total")
        goal = elide(str(payload.get("goal", "")), 70)
        return ("●", f"step {index}/{total} · {goal}", "accent")
    if event_type == "checkpoint_created":
        commit = short_id(str(payload.get("commit")))
        return ("✓", f"checkpoint {commit} · {payload.get('message')}", "success")
    if event_type == "step_accepted":
        files = payload.get("changed_files") or []
        detail = f"{len(files)} files" if files else "no file changes"
        goal = elide(str(payload.get("goal", "")), 60)
        return ("✓", f"step accepted · {detail} · {goal}", "success")
    if event_type == "validation":
        passed = payload.get("passed")
        commands = payload.get("command_results") or []
        failed = [item for item in commands if not item.get("success")]
        if passed:
            return ("✓", f"validation passed · {len(commands)} checks", "success")
        reasons = ", ".join(
            [str(item.get("tool") or "check") for item in failed[:3]]
        ) or "diff check or scope"
        return ("×", f"validation failed · {reasons}", "error")
    if event_type == "evaluation":
        decision = str(payload.get("decision"))
        reason = elide(str(payload.get("reason") or ""), 80)
        if decision == "accept":
            return ("✓", f"accepted · {reason}", "success")
        if decision == "rollback":
            # the richer rollback_completed event follows and reports this
            return None
        if decision == "replan":
            return ("↻", f"replan · {reason}", "warning")
        if decision == "finish_candidate":
            return ("→", f"finish candidate · {reason}", "accent")
        return ("·", f"continue · {reason}", "muted")
    if event_type == "rollback_completed":
        discarded = payload.get("discarded") or []
        reason = elide(str(payload.get("reason") or ""), 60)
        target = short_id(str(payload.get("to_commit")))
        detail = f"{len(discarded)} files discarded" if discarded else "speculative state discarded"
        suffix = f" · {reason}" if reason else ""
        return ("↩", f"rollback · {detail} → {target}{suffix}", "warning")
    if event_type == "replan":
        return ("↻", f"replan · {elide(str(payload.get('reason', '')), 80)}", "warning")
    if event_type == "verification_started":
        return ("●", "final verification started", "accent")
    if event_type == "verification_completed":
        passed = payload.get("passed") and payload.get("hygiene_passed", True)
        criteria = payload.get("criteria") or []
        if passed:
            return ("✓", f"verification passed · {len(criteria)} criteria", "success")
        missing = payload.get("missing_requirements") or []
        detail = elide(", ".join(str(item) for item in missing[:2]), 70) or "hygiene failed"
        return ("×", f"verification failed · {detail}", "error")
    # Model streaming (provider_started / first_token / progress / waiting) is telemetry:
    # it stays in the log screen and in the ACTIVE panel's single state row.  Putting it
    # here buried the semantic story under "streaming controller · 1.2k chars" lines.
    if event_type == "recovery_started":
        trigger = elide(str(payload.get("trigger", "")), 60)
        return ("⟲", f"self-diagnosis #{payload.get('attempt')} · {trigger}", "warning")
    if event_type == "recovery_completed":
        cause = elide(str(payload.get("root_cause", "")), 60)
        strategy = payload.get("strategy")
        if strategy == "replan":
            correction = elide(str(payload.get("corrective_instruction", "")), 60)
            return ("⟲", f"recovery · {cause} → {correction}", "accent")
        return ("⟲", f"recovery · {cause} → {strategy}", "warning")
    if event_type == "recovery_failed":
        error = elide(str(payload.get("error", "")), 60)
        return ("!", f"self-diagnosis unavailable · {error}", "muted")
    if event_type == "model_escalated":
        reason = elide(str(payload.get("reason", "")), 60)
        return ("!", f"escalated to stronger model · {reason}", "warning")
    if event_type == "repetition_detected":
        return ("!", f"repeated {payload.get('tool')} call · evaluation forced", "warning")
    if event_type == "step_budget_exhausted":
        return ("!", f"step budget reached · {payload.get('tool_calls')} tool calls", "warning")
    if event_type == "user_override":
        return ("i", f"instruction · {elide(str(payload.get('text', '')), 70)}", "accent")
    if event_type == "user_question":
        return ("?", f"agent question · {elide(str(payload.get('question', '')), 70)}", "warning")
    if event_type == "run_completed":
        return ("✓", "run complete", "success")
    if event_type == "run_failed":
        return ("×", f"run failed · {elide(str(payload.get('reason', '')), 80)}", "error")
    if event_type == "run_stopped":
        return ("■", "run stopped by user", "muted")
    if event_type == "merge_completed":
        target = payload.get("target_branch") or "the source branch"
        commit = short_id(str(payload.get("merge_commit", "")))
        return ("⇥", f"merged into {target} · {commit}", "success")
    if event_type == "conflict_detected":
        files = payload.get("files") or []
        return ("⚠", f"merge conflict in {len(files)} file(s) · the agent resolves it", "warning")
    if event_type == "conflict_resolved":
        return ("✓", "merge conflict resolved and re-verified", "success")
    if event_type == "conflict_unresolved":
        return ("×", "merge conflict could not be resolved · branch kept for gcae merge", "error")
    if event_type == "repeated_failure":
        return (
            "×",
            f"same failure {payload.get('count')}x · "
            f"{elide(str(payload.get('signature', '')), 60)}",
            "error",
        )
    if event_type == "model_failover":
        role = payload.get("role") or "model"
        return ("⇄", f"{role} failed over to {payload.get('model')}", "warning")
    if event_type == "runtime_degraded":
        error = elide(str(payload.get("error", "")), 60)
        return ("△", f"degraded · {payload.get('component')} · {error}", "warning")
    if event_type == "success_criteria_adopted":
        return ("✓", "success criteria adopted from the diagnosis", "accent")
    if event_type == "rollback_failed":
        return ("×", f"rollback failed · {elide(str(payload.get('reason', '')), 60)}", "error")
    if event_type == "tool_result" and succeeded is False:
        error = payload.get("error") or payload.get("output") or "tool failed"
        return ("×", f"{payload.get('tool')} failed · {elide(str(error), 70)}", "error")
    return None


def log_line(event_type: str, phase: str | None, payload: dict[str, Any]) -> str:
    """Full single-line entry for the detailed log screen."""
    phase_part = f" {phase}" if phase else ""
    if event_type == "tool_result":
        status = "ok" if payload.get("success") else "failed"
        return (
            f"[tool]{phase_part} {payload.get('tool')} {status} "
            f"({payload.get('duration_ms')} ms)"
        )
    if event_type == "decision":
        tool = payload.get("tool")
        name = tool.get("name") if isinstance(tool, dict) else "-"
        return f"[decision]{phase_part} {payload.get('action')} tool={name}"
    if event_type in {
        "provider_started",
        "provider_first_token",
        "provider_progress",
        "provider_waiting",
    }:
        role = payload.get("role")
        model = payload.get("model")
        seconds = (payload.get("elapsed_ms") or 0) / 1000
        characters = payload.get("characters")
        reasoning = payload.get("reasoning_characters")
        detail = f"[model]{phase_part} {event_type.removeprefix('provider_')} role={role}"
        if model:
            detail += f" model={model}"
        if characters is not None:
            detail += f" chars={characters}"
        if reasoning:
            detail += f" reasoning={reasoning}"
        if seconds:
            detail += f" {seconds:.1f}s"
        if payload.get("preview"):
            detail += f" preview={elide(str(payload['preview']), 60)}"
        return detail
    if event_type == "context_built":
        return (
            f"[context]{phase_part} {payload.get('estimated_tokens')} tok (est) "
            f"{payload.get('characters')} chars pinned={payload.get('pinned')} "
            f"omitted={payload.get('omitted')}"
        )
    if event_type == "memory_updated":
        return f"[memory]{phase_part} {payload.get('counts')}"
    if event_type == "candidate_state":
        files = len(payload.get("files") or [])
        return (
            f"[git]{phase_part} dirty={payload.get('dirty')} files={files} "
            f"+{payload.get('added')} -{payload.get('deleted')}"
        )
    if event_type in {"validation", "evaluation", "verification_completed"}:
        return f"[{event_type}]{phase_part} {elide(str(payload), 160)}"
    return f"[{event_type}]{phase_part}"


def context_sections(text: str) -> list[tuple[str, int, str, str]]:
    """Labelled sections of the real request payload: (name, chars, preview, content)."""
    prefixes: tuple[tuple[str, str], ...] = (
        ("Objective:", "objective"),
        ("Original request:", "original request"),
        ("Hard constraints:", "hard constraints"),
        ("Success criteria:", "success criteria"),
        ("Accepted commit:", "checkpoint"),
        ("Current goal:", "current goal"),
        ("Latest user instruction:", "user instruction"),
        ("Step tool calls:", "step budget"),
        ("Plan:", "plan"),
        ("Active files:", "active files"),
        ("Working ", "working memory"),
        ("Memory[", "retrieved memory"),
        ("Observation:", "observations"),
        ("Current diff:", "diff"),
        ("Validation ", "validation"),
    )
    sections: list[tuple[str, str, list[str]]] = []
    current_name = "pinned instructions"
    buffer: list[str] = []

    def flush(name: str, lines: list[str]) -> None:
        if lines:
            sections.append((name, "\n".join(lines), lines[:]))

    for line in text.splitlines():
        name = next((label for prefix, label in prefixes if line.startswith(prefix)), None)
        if name is not None:
            flush(current_name, buffer)
            current_name = name
            buffer = [line]
        else:
            buffer.append(line)
    flush(current_name, buffer)

    merged: dict[str, list[Any]] = {}
    order: list[str] = []
    for name, content, _lines in sections:
        if name not in merged:
            merged[name] = [0, []]
            order.append(name)
        merged[name][0] = int(merged[name][0]) + len(content)
        merged[name][1].append(content)
    return [
        (
            name,
            int(merged[name][0]),
            str(merged[name][1][0]).splitlines()[0],
            "\n".join(str(chunk) for chunk in merged[name][1]),
        )
        for name in order
    ]


def render_diff(text: str, max_lines: int = 4000) -> Text:
    """Colourised unified diff, bounded in length."""
    body = Text()
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if index >= max_lines:
            body.append(f"… diff truncated at {max_lines} lines\n", style=STYLES["muted"])
            break
        if line.startswith(("+++", "---")) or line.startswith("diff --git"):
            style = STYLES["title"]
        elif line.startswith("@@"):
            style = STYLES["accent"]
        elif line.startswith("+"):
            style = STYLES["success"]
        elif line.startswith("-"):
            style = STYLES["error"]
        else:
            style = ""
        body.append(line + "\n", style=style)
    return body or Text("(no changes)", style=STYLES["muted"])
