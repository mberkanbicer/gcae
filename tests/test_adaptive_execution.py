"""Adaptive, execution-driven behaviour: the runtime must observe, adapt, and verify.

The tests here are behavioural: they drive the real ``Runtime`` against real processes and
assert that what actually happened changes what happens next.  Nothing is asserted about
prompt wording — where the runtime enforces something (refusing a blind repeat, requiring
evidence, waiting for user input) the assertion is on the runtime's own decision.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from gcae.execution import (
    CommandRequest,
    CommandRunner,
    ExecutionMode,
    FailureKind,
    TimeoutKind,
    classify_failure,
    prompt_line,
    strategy_signature,
)
from gcae.models import (
    Event,
    InitialPlan,
    PlanStep,
)
from gcae.persistence import StateStore
from gcae.planner import Planner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl

# ------------------------------------------------------------------ fixtures


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class ScriptedPlanner(Planner):
    """A planner that returns exactly the steps a test wants, without a model call."""

    def __init__(self, steps: list[PlanStep]) -> None:
        self.steps = steps

    def plan(self, state: Any) -> InitialPlan:
        return InitialPlan(
            objective=state.objective,
            success_criteria=list(state.success_criteria),
            steps=list(self.steps),
        )

    def replan(self, state: Any, reason: str) -> list[PlanStep]:
        del state, reason
        return []


def one_step_plan(goal: str = "verify the program") -> ScriptedPlanner:
    return ScriptedPlanner([PlanStep(id="step-1", goal=goal)])


def collect(runtime: Runtime) -> list[Event]:
    events: list[Event] = []
    runtime.subscribe(events.append)
    return events


def make_runtime(
    tmp_path: Path,
    provider: Any,
    *,
    planner: Planner | None = None,
    criteria: list[str] | None = None,
    **kwargs: Any,
) -> Runtime:
    source = tmp_path / "source"
    source.mkdir(parents=True, exist_ok=True)
    if not (source / ".git").exists():
        init_repo(source)
    for extra in sorted(tmp_path.glob("seed/*")):
        (source / extra.name).write_bytes(extra.read_bytes())
        subprocess.run(["git", "-C", str(source), "add", extra.name], check=True)
    if any(tmp_path.glob("seed/*")):
        subprocess.run(
            ["git", "-C", str(source), "-c", "user.email=t@e.f", "-c", "user.name=T",
             "commit", "-qm", "seed"], check=True,
        )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=planner or one_step_plan(),
        control=RuntimeControl(),
        **kwargs,
    )
    runtime.start("do the work", success_criteria=criteria or ["file exists: README.md"])
    return runtime


# ------------------------------------------------------------------ D/E: execution modes


def test_scripted_input_feeds_a_prompting_program(tmp_path: Path) -> None:
    """D: a program that asks questions completes when the answers are supplied."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "hello.py").write_text("name = input('Name: ')\nprint(f'Hello {name}')\n")
    runner = CommandRunner(work, default_timeout=10, idle_timeout=5)

    outcome = runner.run(
        CommandRequest(
            command="python hello.py",
            mode=ExecutionMode.SCRIPTED_INPUT,
            stdin=["Ada"],
        )
    )

    assert not outcome.timed_out, "scripted input must not time out"
    assert outcome.exit_code == 0
    assert "Hello Ada" in outcome.stdout
    assert outcome.stdin_sent == 1


def test_multiple_scripted_guesses_complete(tmp_path: Path) -> None:
    """E: several answers are consumed and the process is seen to finish."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "game.py").write_text(
        "secret = 7\n"
        "for _ in range(3):\n"
        "    guess = int(input('Your guess: '))\n"
        "    print('low' if guess < secret else 'high' if guess > secret else 'correct')\n"
    )
    runner = CommandRunner(work, default_timeout=10, idle_timeout=5)
    outcome = runner.run(
        CommandRequest(
            command="python game.py",
            mode=ExecutionMode.SCRIPTED_INPUT,
            stdin=["3", "9", "7"],
        )
    )
    assert outcome.exit_code == 0, outcome.combined
    assert outcome.stdin_sent == 3
    assert "low" in outcome.stdout and "high" in outcome.stdout and "correct" in outcome.stdout


def test_interactive_pty_gives_the_program_a_terminal(tmp_path: Path) -> None:
    """F: PTY mode when the program checks for a terminal; batch mode must not pretend."""
    if sys.platform == "win32":  # pragma: no cover - PTY is POSIX-only
        pytest.skip("PTY is not available on this platform")
    work = tmp_path / "work"
    work.mkdir()
    (work / "tty.py").write_text("import sys\nprint('tty' if sys.stdin.isatty() else 'pipe')\n")
    runner = CommandRunner(work, default_timeout=10, idle_timeout=5)

    pty_outcome = runner.run(
        CommandRequest(command="python tty.py", mode=ExecutionMode.INTERACTIVE_PTY)
    )
    batch_outcome = runner.run(CommandRequest(command="python tty.py"))

    assert "tty" in pty_outcome.stdout
    assert "pipe" in batch_outcome.stdout


# ------------------------------------------------------------------ I: timeouts


def test_a_hung_command_is_classified_and_killed_with_its_output(tmp_path: Path) -> None:
    """I: a real hang is an idle timeout with partial evidence, not a silent failure."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "hang.py").write_text("import time\nprint('working')\nwhile True: time.sleep(0.5)\n")
    runner = CommandRunner(work, default_timeout=30, idle_timeout=0.8, startup_timeout=5)

    started = time.monotonic()
    outcome = runner.run(CommandRequest(command="python hang.py", timeout=30, idle_timeout=0.8))
    elapsed = time.monotonic() - started

    assert outcome.timed_out
    assert outcome.timeout_kind is TimeoutKind.IDLE
    assert "working" in outcome.stdout, "partial output must be captured"
    assert outcome.termination_reason, "the termination must be recorded"
    assert elapsed < 5, "the idle timeout must fire long before the wall clock limit"
    # the process group is gone, not orphaned
    time.sleep(0.2)
    orphans = subprocess.run(["pgrep", "-f", "hang.py"], capture_output=True)
    assert orphans.returncode != 0, "the hung process survived the timeout"


def test_a_prompting_program_is_not_a_generic_timeout(tmp_path: Path) -> None:
    """G: batch mode discovers interactivity and classifies it, instead of "timed out"."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "ask.py").write_text("value = input('Enter your name: ')\nprint(value)\n")
    runner = CommandRunner(work, default_timeout=10, idle_timeout=0.8, startup_timeout=5)

    outcome = runner.run(CommandRequest(command="python ask.py", timeout=10, idle_timeout=0.8))

    assert outcome.failure_kind is FailureKind.INTERACTIVE_INPUT_REQUIRED
    assert outcome.interactive_detected
    assert outcome.prompt, "the question the program asked must be kept"
    assert not outcome.timed_out, "closing stdin must produce evidence, not a false timeout"


def test_prompt_detection_recognises_questions() -> None:
    assert prompt_line("Enter the API token: ")
    assert prompt_line("Choose an option [1-3]:")
    assert prompt_line("Continue? (y/n)")
    assert not prompt_line("Tests passed: 12")
    assert not prompt_line("")


def test_failure_classification_covers_the_useful_cases() -> None:
    assert classify_failure(exit_code=0) is FailureKind.NONE
    assert (
        classify_failure(exit_code=1, stderr="ModuleNotFoundError: no module named 'x'")
        is FailureKind.DEPENDENCY_MISSING
    )
    assert classify_failure(exit_code=2, stdout="2 failed, 1 passed") is FailureKind.TEST_FAILURE
    assert (
        classify_failure(exit_code=1, stderr="Traceback ... SyntaxError: bad")
        is FailureKind.CODE_ERROR
    )
    assert (
        classify_failure(exit_code=1, stderr="cat: x: No such file or directory")
        is FailureKind.FILE_NOT_FOUND
    )
    assert (
        classify_failure(exit_code=1, timeout_kind=None, timed_out=True)
        is FailureKind.COMMAND_TIMEOUT
    )


def test_strategy_signature_ignores_meaningless_changes() -> None:
    same = strategy_signature("python game.py", "Error: boom\nline 2")
    noisier = strategy_signature("python   game.py ", "Error: boom\nother context")
    assert same == noisier, "whitespace and trailing detail must not create a new strategy"
    assert strategy_signature("python game.py 100", "Error: boom") != same


# ------------------------------------------------------------------ H: real user input


def test_a_process_waiting_for_real_input_asks_instead_of_timing_out(tmp_path: Path) -> None:
    """H: the run waits for the user, then continues with the answer."""
    if sys.platform == "win32":  # pragma: no cover
        pytest.skip("PTY is not available on this platform")
    program = (
        "token = input('Enter API token: ')\n"
        "print(f'token length {len(token)}')\n"
    )
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "run the program",
                "reason_summary": "it needs a token",
                "expected_result": "the program reports the token length",
                "tool": {
                    "name": "run_command",
                    "arguments": {
                        "command": f"python -c \"{program}\"",
                        "mode": "interactive_pty",
                        "interactive": True,
                    },
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "run the program",
                "reason_summary": "verified",
            },
        ]
    )
    runtime = make_runtime(tmp_path, provider)
    events = collect(runtime)
    state = runtime.run()

    assert state.status == "waiting_for_user", state.status
    assert state.pending_input is not None
    assert "token" in state.pending_input.prompt.lower()
    assert state.pending_input.sensitive, "a token prompt must be treated as sensitive"
    kinds = [event.event_type for event in events]
    assert "interactive_input_required" in kinds
    # persisted, so a resumed run still knows what it was waiting for
    persisted = StateStore(runtime._run_dir() / "state.json").load()  # noqa: SLF001
    assert persisted.pending_input is not None

    runtime.submit_process_input("abcdef")
    resumed = runtime.run()

    assert resumed.status == "complete", resumed.status
    assert any("user_input_supplied" in event.event_type for event in events)
    assert resumed.pending_input is None
    # the value itself never reaches the durable record
    memories = runtime.memory.all(resumed.run_id) if runtime.memory is not None else []
    assert not any("abcdef" in record.content for record in memories)


# ------------------------------------------------------------------ A: no blind repeat


def test_a_failing_command_is_not_retried_unchanged(tmp_path: Path) -> None:
    """A/C: the same approach failing the same way is refused, not repeated."""
    script = "import sys\nprint('boom')\nsys.exit(1)\n"
    repeated = {
        "action": "execute_tool",
        "semantic_goal": "run it",
        "reason_summary": "try the command",
        "expected_result": "it works",
        "tool": {
            "name": "run_command",
            "arguments": {"command": f"python -c \"{script}\"", "mode": "batch"},
        },
    }
    # the controller keeps asking for the identical command: the runtime must stop it
    provider = FakeProvider([repeated] * 8)
    runtime = make_runtime(
        tmp_path,
        provider,
        planner=one_step_plan("run the failing program"),
        max_steps=8,
        recovery_attempts=0,
        strategy_retry_limit=2,
    )
    events = collect(runtime)
    state = runtime.run()

    executed = [
        event
        for event in events
        if event.event_type == "tool_result" and event.payload.get("tool") == "run_command"
    ]
    refused = [
        event
        for event in events
        if event.event_type == "tool_result" and event.payload.get("mode") == "refused"
    ]
    assert len(executed) - len(refused) <= 2, "the identical failing command ran too often"
    assert refused or any(event.event_type == "repetition_detected" for event in events), (
        "the third identical attempt must be prevented"
    )
    assert state.last_failure is not None
    memory = runtime.memory.all(state.run_id) if runtime.memory is not None else []
    assert any(
        record.kind == "failure" and "asks for input" in record.content
        or record.kind == "failure" and "boom" in record.content
        for record in memory
    ), "the failure must be remembered"


def test_a_retry_after_a_real_change_is_allowed(tmp_path: Path) -> None:
    """The refusal must not block a retry that changed something meaningful."""
    script = "import sys\nsys.exit(1)\n"
    command = f"python -c \"{script}\""
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "first attempt",
                "tool": {"name": "run_command", "arguments": {"command": command}},
            },
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "same command, but the code changed in between",
                "tool": {"name": "create_file", "arguments": {"path": "fix.txt", "content": "x"}},
            },
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "retry the command",
                "tool": {"name": "run_command", "arguments": {"command": command}},
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "run it",
                "reason_summary": "enough",
            },
        ]
    )
    runtime = make_runtime(
        tmp_path,
        provider,
        planner=one_step_plan("run it"),
        max_steps=6,
        recovery_attempts=0,
        strategy_retry_limit=1,
    )
    events = collect(runtime)
    runtime.run()

    refused = [
        event
        for event in events
        if event.event_type == "tool_result" and event.payload.get("mode") == "refused"
    ]
    executed_commands = [
        event
        for event in events
        if event.event_type == "tool_result"
        and event.payload.get("tool") == "run_command"
        and event.payload.get("mode") != "refused"
    ]
    assert not refused, "a retry after a real change must be allowed"
    assert len(executed_commands) == 2


# ------------------------------------------------------------------ B: repair after evidence


def test_a_failed_check_reaches_the_context_and_the_code_is_repaired(tmp_path: Path) -> None:
    """B: the failure output enters the next decision and the repair is what gets accepted."""
    # the first version really crashes (IndexError); the repair really runs
    bad = "names = []\nprint(names[1])\n"
    good = "names = []\nprint(names[0] if names else 'empty')\n"
    worktree_seed = tmp_path / "seed"
    worktree_seed.mkdir()
    (worktree_seed / "prog.py").write_text(bad)
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "first attempt",
                "tool": {
                    "name": "run_command",
                    "arguments": {"command": "python prog.py"},
                },
            },
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "fix the crash the run reported",
                "tool": {"name": "write_file", "arguments": {"path": "prog.py", "content": good}},
            },
            {
                "action": "execute_tool",
                "semantic_goal": "run it",
                "reason_summary": "run the repaired program",
                "tool": {"name": "run_command", "arguments": {"command": "python prog.py"}},
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "run it",
                "reason_summary": "it runs now",
            },
        ]
    )
    runtime = make_runtime(tmp_path, provider, planner=one_step_plan("run it"), max_steps=6)
    events = collect(runtime)
    state = runtime.run()

    assert (tmp_path / "runtime").exists()
    worktree = Path(state.worktree)
    assert (worktree / "prog.py").read_text() == good
    results = [
        event.payload for event in events if event.event_type == "tool_result"
    ]
    first_run = next(item for item in results if item.get("tool") == "run_command")
    assert first_run["exit_code"] == 1, "the failing run must be observed as a failure"
    assert any(event.event_type == "failure_classified" for event in events)
    last_run = [item for item in results if item.get("tool") == "run_command"][-1]
    assert last_run["exit_code"] == 0, "the repaired program must actually run"
    assert state.accepted_steps >= 1


# ------------------------------------------------------------------ J: verification


def test_completion_requires_the_criteria_to_hold(tmp_path: Path) -> None:
    """J: an implementation that does not satisfy the criterion never completes."""
    provider = FakeProvider(
        [{"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "done"}]
    )
    runtime = make_runtime(
        tmp_path,
        provider,
        planner=one_step_plan("write the answer"),
        criteria=["file exists: answer.txt"],
        max_steps=4,
        recovery_attempts=0,
    )
    events = collect(runtime)
    state = runtime.run()

    assert state.status != "complete", "a missing criterion must not complete the run"
    verification = [event for event in events if event.event_type == "verification_completed"]
    assert verification, "the run must actually verify before finishing"
    assert not verification[-1].payload["passed"]
    assert not event_is_complete(events)

    # the same repository, with the required artifact present, verifies
    fixed = make_runtime(
        tmp_path / "second",
        FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "write it",
                    "reason_summary": "write the required artifact",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "answer.txt", "content": "answer\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "write it",
                    "reason_summary": "done",
                },
                {"action": "finish_candidate", "semantic_goal": "finish", "reason_summary": "done"},
            ]
        ),
        planner=one_step_plan("write the answer"),
        criteria=["file exists: answer.txt"],
        max_steps=6,
        recovery_attempts=0,
    )
    assert fixed.run().status == "complete"


def event_is_complete(events: list[Event]) -> bool:
    return any(event.event_type == "run_completed" for event in events)


# ------------------------------------------------------------------ evidence gate


class AlwaysAccepts:
    """A credulous evaluator: it would accept plausible code with no execution behind it."""

    def evaluate(self, payload: Any) -> Any:
        from gcae.models import Evaluation

        del payload
        return Evaluation(decision="accept", reason="looks plausible", progress_score=1.0)


def test_code_without_execution_evidence_is_not_accepted(tmp_path: Path) -> None:
    """The runtime enforces "run it" instead of trusting a credulous acceptance."""
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "implement",
                "reason_summary": "write the module",
                "tool": {
                    "name": "write_file",
                    "arguments": {"path": "app.py", "content": "print('hello')\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "implement",
                "reason_summary": "looks right",
            },
            {
                "action": "execute_tool",
                "semantic_goal": "implement",
                "reason_summary": "actually run it",
                "tool": {"name": "run_command", "arguments": {"command": "python app.py"}},
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "implement",
                "reason_summary": "it ran",
            },
        ]
    )
    runtime = make_runtime(
        tmp_path,
        provider,
        planner=one_step_plan("implement the module"),
        criteria=["command succeeds: python app.py"],
        max_steps=6,
        recovery_attempts=0,
        require_execution_evidence=True,
        validator_commands=[],
        evaluator=AlwaysAccepts(),
    )
    events = collect(runtime)
    state = runtime.run()

    assert any(event.event_type == "execution_evidence_required" for event in events), (
        "writing code without running it must not be accepted"
    )
    assert state.step_commands >= 1, "the agent then actually ran the program"
    assert state.accepted_steps >= 1, "and the step was accepted once there was evidence"


def test_the_gate_follows_the_configuration(tmp_path: Path) -> None:
    """With the gate off, a documentation-style step is accepted on its text alone."""
    provider = FakeProvider(
        [
            {
                "action": "execute_tool",
                "semantic_goal": "write",
                "reason_summary": "write the module",
                "tool": {
                    "name": "write_file",
                    "arguments": {"path": "app.py", "content": "print('hi')\n"},
                },
            },
            {
                "action": "complete_semantic_step",
                "semantic_goal": "write",
                "reason_summary": "done",
            },
        ]
    )
    runtime = make_runtime(
        tmp_path,
        provider,
        planner=one_step_plan("write the module"),
        criteria=["file exists: app.py"],
        max_steps=4,
        recovery_attempts=0,
        require_execution_evidence=False,
    )
    events = collect(runtime)
    runtime.run()
    assert not [e for e in events if e.event_type == "execution_evidence_required"]


# ------------------------------------------------------------------ blocking


def test_a_run_with_no_autonomous_path_left_is_blocked_not_failed(tmp_path: Path) -> None:
    """BLOCKED says what is missing and what would unblock it."""
    runtime = make_runtime(tmp_path, FakeProvider([]))
    events = collect(runtime)
    state = runtime.block("the deploy needs a token this run cannot obtain", "provide DEPLOY_TOKEN")

    assert state.status == "blocked"
    assert state.blocked_reason
    assert state.unblock_hint
    payload = next(event.payload for event in events if event.event_type == "run_blocked")
    assert "token" in payload["reason"] and payload["unblock"]


# ------------------------------------------------------------------ CLI surface


def test_the_cli_can_supply_input_to_a_waiting_run(tmp_path: Path) -> None:
    """`gcae input` records the answer for a run whose process did not survive a restart."""
    from gcae.cli import main

    runtime = make_runtime(tmp_path, FakeProvider([]))
    assert runtime.state is not None
    from gcae.models import PendingInput

    runtime.state.pending_input = PendingInput(
        command="python deploy.py",
        prompt="Enter deploy token: ",
        goal="deploy",
        mode="interactive_pty",
        sensitive=True,
    )
    runtime.state.status = "waiting_for_user"
    runtime._persist()  # noqa: SLF001
    run_id = runtime.state.run_id
    runtime.release_lock()

    config = tmp_path / "config.toml"
    config.write_text(
        f'[provider]\nkind = "fake"\n\n[runtime]\nstate_dir = "{tmp_path / "runtime"}"\n'
    )
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "input",
                str(tmp_path / "source"),
                run_id,
                "token-value",
                "--config",
                str(config),
            ]
        )
    # the run had no live process: the answer is kept as an instruction and the run stops
    # without a process rather than hanging
    assert exit_info.value.code in {0, 1}
    resumed = StateStore(tmp_path / "runtime" / "runs" / run_id / "state.json").load()
    assert resumed.pending_input is None, "the answer must clear the pending request"
    assert resumed.latest_user_instruction and "deploy.py" in resumed.latest_user_instruction
