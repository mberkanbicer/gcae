"""Progress events: semantic lines for the main UI, never raw telemetry."""

from gcae.progress import ProgressCategory, ProgressFeed, ProgressStatus, summarize_event


def _decision(reason: str, name: str, arguments: dict) -> dict:
    return {
        "reason_summary": reason,
        "tool": {"name": name, "arguments": arguments},
    }


def test_telemetry_never_becomes_progress() -> None:
    for event_type in (
        "provider_started",
        "provider_first_token",
        "provider_progress",
        "provider_waiting",
        "provider_finished",
        "context_built",
        "candidate_state",
        "memory_updated",
        "phase:execute",
    ):
        assert summarize_event(event_type, {}) is None


def test_tool_decisions_map_to_operational_categories() -> None:
    read = summarize_event("decision", _decision("look", "read_file", {"path": "game.py"}))
    assert read is not None and read.category is ProgressCategory.INSPECT
    assert read.related_file == "game.py"
    edit = summarize_event(
        "decision", _decision("fix init", "write_file", {"path": "game.py"})
    )
    assert edit is not None and edit.category is ProgressCategory.EDIT
    run = summarize_event(
        "decision",
        _decision("run it", "run_command", {"command": "python game.py"}),
    )
    assert run is not None and run.category is ProgressCategory.EXECUTE
    assert run.related_command == "python game.py"
    assert run.status is ProgressStatus.RUNNING
    tests = summarize_event("decision", _decision("t", "run_tests", {}))
    assert tests is not None and tests.category is ProgressCategory.TEST


def test_consecutive_reads_group_into_one_inspect_line() -> None:
    feed = ProgressFeed()
    assert feed.push("decision", _decision("", "read_file", {"path": "a.py"})) == []
    assert feed.push("decision", _decision("", "read_file", {"path": "b.py"})) == []
    flushed = feed.push(
        "decision",
        _decision("", "run_command", {"command": "pytest -q"}),
    )
    assert len(flushed) == 2
    grouped, running = flushed
    assert grouped.category is ProgressCategory.INSPECT
    assert "2 relevant files" in grouped.title
    assert "a.py" in grouped.summary and "b.py" in grouped.summary
    assert grouped.status is ProgressStatus.OK


def test_single_read_stays_a_single_line() -> None:
    feed = ProgressFeed()
    assert feed.push("decision", _decision("", "read_file", {"path": "a.py"})) == []
    flushed = feed.push(
        "decision", _decision("", "write_file", {"path": "a.py"})
    )
    assert flushed[0].category is ProgressCategory.INSPECT
    assert "a.py" in flushed[0].title + flushed[0].summary


def test_tool_result_resolves_the_open_execute_line() -> None:
    feed = ProgressFeed()
    feed.push(
        "decision",
        _decision("", "run_command", {"command": "python game.py"}),
        step_id="step-1",
    )
    resolved = feed.resolve("run_command", "step-1", False, "FAILED · curses.error")
    assert resolved is not None
    assert resolved.status is ProgressStatus.FAIL
    assert "curses.error" in resolved.result
    assert len(feed.events) == 1, "resolution updates the line, it does not add one"


def test_failure_and_recovery_lines_carry_explanation() -> None:
    failure = summarize_event(
        "trajectory_step_completed",
        {
            "status": "rejected",
            "decision_reason": "batch waited for stdin",
            "semantic_goal": "run",
        },
    )
    assert failure is not None and failure.category is ProgressCategory.FAILURE
    assert "batch waited for stdin" in failure.title
    recovery = summarize_event(
        "recovery_completed",
        {
            "root_cause": "empty controller output",
            "corrective_instruction": "retry compact",
            "strategy": "replan",
        },
    )
    assert recovery is not None and recovery.category is ProgressCategory.RECOVER
    assert "empty controller output" in recovery.title
    assert "retry compact" in recovery.title


def test_routine_continue_and_rollback_vote_are_not_progress() -> None:
    assert summarize_event("evaluation", {"decision": "continue", "reason": "x"}) is None
    assert summarize_event("evaluation", {"decision": "rollback", "reason": "x"}) is None
    assert summarize_event("evidence_recorded", {"supports": ["x"]}) is None
    contradiction = summarize_event(
        "evidence_recorded",
        {"claim": "restart works", "contradicts": ["restart works"], "summary": "exited"},
    )
    assert contradiction is not None and contradiction.category is ProgressCategory.FAILURE


def test_semantic_progress_reads_like_an_engineering_journal() -> None:
    """Required granularity: a full trajectory as operational lines, no telemetry."""
    from datetime import UTC, datetime

    feed = ProgressFeed()
    at = datetime(2026, 1, 1, 12, 3, 1, tzinfo=UTC)
    script: list[tuple[str, dict]] = [
        ("plan_updated", {"reason": "initial plan", "steps": [{}, {}]}),
        ("decision", {"tool": {"name": "read_file", "arguments": {"path": "a.py"}}}),
        ("decision", {"tool": {"name": "read_file", "arguments": {"path": "b.py"}}}),
        ("decision", {"tool": {"name": "read_file", "arguments": {"path": "c.py"}}}),
        (
            "decision",
            {"tool": {"name": "write_file", "arguments": {"path": "game.py"}},
             "reason_summary": "Updating game logic"},
        ),
        ("decision", {"tool": {"name": "run_command", "arguments": {"command": "python game.py"}}}),
    ]
    shown: list[str] = []
    for kind, payload in script:
        for line in feed.push(kind, payload, step_id="step-1", timestamp=at):
            shown.append(f"{line.category.value.upper()} {line.title}")
    resolved = feed.resolve("run_command", "step-1", False, "FAILED · curses.error")
    assert resolved is not None
    shown.append(f"{resolved.category.value.upper()} {resolved.title} · {resolved.result}")
    for line in feed.push("failure_classified", {"lesson": "needs a terminal"}, step_id="step-1"):
        shown.append(f"{line.category.value.upper()} {line.title}")
    rolled = feed.push(
        "rollback_completed",
        {"to_commit": "c00a1ba", "discarded": ["game.py"]},
        step_id="step-1",
    )
    for line in rolled:
        shown.append(f"{line.category.value.upper()} {line.title}")
    for line in feed.push("replan", {"reason": "use PTY execution"}, step_id="step-1"):
        shown.append(f"{line.category.value.upper()} {line.title}")
    for line in feed.push(
        "validation",
        {"passed": True, "command_results": [{"tool": "run_command", "success": True}]},
        step_id="step-2",
    ):
        shown.append(f"{line.category.value.upper()} {line.title}")
    for line in feed.push(
        "checkpoint_created",
        {"commit": "7fa89c2", "message": "curses initialization fixed"},
        step_id="step-2",
    ):
        shown.append(f"{line.category.value.upper()} {line.title}")
    text = "\n".join(shown)
    for expected in (
        "PLAN", "INSPECT Reviewed 3 relevant files", "EDIT", "EXECUTE",
        "FAILED · curses.error", "DIAGNOSE", "ROLLBACK", "REPLAN",
        "VALIDATE Validation passed · 1 checks", "ACCEPT Checkpoint 7fa89c2",
    ):
        assert expected in text, expected
    assert "reasoning" not in text and "chars=" not in text
    assert "read_file" not in text, "no per-file callback lines"


def test_context_warning_is_one_deliberate_line() -> None:
    """§28: the anomaly reaches the user as one SYSTEM line, never as raw telemetry."""
    feed = ProgressFeed()
    lines = feed.push(
        "context_warning",
        {"memory_share": 0.44, "dropped_duplicates": 12, "retrieved": 31},
        step_id="step-1",
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.category.value == "system"
    assert "44%" in line.title and "12 duplicates" in line.title
    assert "reasoning" not in line.title
