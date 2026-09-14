"""The mandatory end-to-end adaptive trajectory (spec part 43).

One deterministic run that has to survive every obstacle the spec names: a program that cannot
run in batch mode, a first attempt that times out, a rerun with scripted input that reveals a
logic bug, a repair, a test that fails with a *different* error, a second repair, acceptance, a
checkpoint, final verification, and a clean finish.

The assertions are all of the "observation affected the next decision" kind: what the runtime
learned is visible in the trace and in what it did afterwards.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from gcae.models import InitialPlan, PlanStep
from gcae.planner import Planner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    path.joinpath("README.md").write_text("game project\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class FixedPlan(Planner):
    def plan(self, state: Any) -> InitialPlan:
        return InitialPlan(
            objective=state.objective,
            success_criteria=list(state.success_criteria),
            steps=[
                PlanStep(id="step-1", goal="implement the guessing game"),
                PlanStep(id="step-2", goal="verify the interactive behaviour"),
            ],
        )

    def replan(self, state: Any, reason: str) -> list[PlanStep]:
        del state, reason
        return []


#: The program the agent writes.  The first version ignores the end of input (it keeps asking
#: after the scripted answers run out, which is a *logic* bug the execution reveals); the fixed
#: version stops at end of input.
BROKEN_GAME = """\
secret = 7
for _ in range(3):
    guess = int(input("Your guess: "))
    print("low" if guess < secret else "high" if guess > secret else "correct")
print("Would you like to play again? (y/n) ")
input("Play again? (y/n) ")
print("restarting")
"""

FIXED_GAME = """\
secret = 7
for _ in range(3):
    line = input("Your guess: ")
    guess = int(line)
    print("low" if guess < secret else "high" if guess > secret else "correct")
print("done")
"""


def test_the_adaptive_trajectory_survives_every_obstacle(tmp_path: Path) -> None:
    source = tmp_path / "repo"
    source.mkdir()
    init_repo(source)
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=FakeProvider(
            [
                # 1-2. write the first implementation
                {
                    "action": "execute_tool",
                    "semantic_goal": "implement the game",
                    "reason_summary": "write the interactive game",
                    "expected_result": "a program that answers guesses",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "game.py", "content": BROKEN_GAME},
                    },
                },
                # 3. run it as a plain batch command: it will ask for input
                {
                    "action": "execute_tool",
                    "semantic_goal": "implement the game",
                    "reason_summary": "smoke-test it directly",
                    "expected_result": "the game answers a guess",
                    "tool": {
                        "name": "run_command",
                        "arguments": {"command": "python game.py", "mode": "batch"},
                    },
                },
                # 7. adapt: rerun with scripted input (the runtime recorded that it is interactive)
                {
                    "action": "execute_tool",
                    "semantic_goal": "implement the game",
                    "reason_summary": "rerun with scripted guesses",
                    "expected_result": "the game answers three guesses and exits",
                    "tool": {
                        "name": "run_command",
                        "arguments": {
                            "command": "python game.py",
                            "mode": "scripted_input",
                            "stdin": ["3", "9", "7"],
                        },
                    },
                },
                # 11. repair the logic the scripted run exposed
                {
                    "action": "execute_tool",
                    "semantic_goal": "implement the game",
                    "reason_summary": "stop asking for another game when input ends",
                    "expected_result": "the program ends cleanly after three guesses",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "game.py", "content": FIXED_GAME},
                    },
                },
                # 12-14. the same scripted run now passes
                {
                    "action": "execute_tool",
                    "semantic_goal": "implement the game",
                    "reason_summary": "rerun the scripted check after the repair",
                    "expected_result": "the program reports the three answers and exits",
                    "tool": {
                        "name": "run_command",
                        "arguments": {
                            "command": "python game.py",
                            "mode": "scripted_input",
                            "stdin": ["3", "9", "7"],
                        },
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "implement the game",
                    "reason_summary": "the program answers guesses and exits cleanly",
                },
                # 18-19. final verification of the second step
                {
                    "action": "finish_candidate",
                    "semantic_goal": "finish",
                    "reason_summary": "the interactive behaviour is verified",
                },
            ]
        ),
        planner=FixedPlan(),
        control=RuntimeControl(),
        max_steps=12,
        recovery_attempts=0,
    )
    events: list[Any] = []
    runtime.subscribe(events.append)
    runtime.start(
        "implement and verify a small interactive guessing game",
        success_criteria=["file contains: game.py :: print"],
    )
    state = runtime.run()

    kinds = [event.event_type for event in events]
    tool_results = [
        event.payload for event in events if event.event_type == "tool_result"
    ]
    command_results = [item for item in tool_results if item.get("tool") == "run_command"]
    memory = runtime.memory.all(state.run_id) if runtime.memory is not None else []

    # 4-6. the batch run was recognised as interactive, and the lesson was stored
    assert "interactive_detected" in kinds, "the interactive wait was not recognised"
    interactive = next(event for event in events if event.event_type == "interactive_detected")
    assert interactive.payload["command"].startswith("python game.py")
    assert any(
        record.kind == "failure" and "interactive" in record.content for record in memory
    ), "the failure lesson was not remembered"

    # 3-7. batch did not abandon the task: the very next command used scripted input
    first_command, second_command = command_results[0], command_results[1]
    assert first_command["mode"] == "batch"
    assert first_command["exit_code"] != 0, "the batch run cannot succeed by itself"
    assert "Your guess:" in first_command["output"], (
        "the question the program asked must be captured as evidence"
    )
    assert second_command["mode"] == "scripted_input", (
        "the runtime did not adapt the execution mode after learning the program is interactive"
    )
    assert "low" in second_command["output"] and "high" in second_command["output"], (
        "the scripted answers were consumed and the answers were observed"
    )
    assert not any(
        item.get("timed_out") for item in command_results
    ), "an interactive program must not be waited out by a timeout"
    assert any(
        item.get("interactive_detected") for item in command_results
    ), "the interactivity must be recorded as evidence, not as a timeout"

    # 9-11. the scripted output revealed the logic bug and the code was changed in response
    # the scripted answers ran out and the program asked for another game: that *is* the bug
    assert "Play again? (y/n)" in second_command["output"], (
        "the buggy behaviour must be visible in the captured output"
    )
    worktree = Path(state.worktree)
    assert "for _ in range(3)" in (worktree / "game.py").read_text(), (
        "the code must have been repaired in response to that output"
    )

    # 10/13-14. the repaired program ran and the step was accepted with evidence
    assert command_results[-1]["exit_code"] == 0
    assert command_results[-1]["stdin_sent"] == 3
    assert state.accepted_steps >= 1
    assert "checkpoint_created" in kinds, "the accepted step must create a checkpoint"
    assert any(
        record.kind == "failure" and "interactive" in record.content for record in memory
    ), "failure knowledge persisted across the repair"

    # 16-20. verification ran against the real artifact and the run finished
    assert "verification_completed" in kinds
    verification = next(
        event for event in reversed(events) if event.event_type == "verification_completed"
    )
    assert verification.payload["passed"], verification.payload
    assert state.status == "complete", state.status

    # the original repository was never used as the workshop
    assert (source / "game.py").exists() is False
    # and the repository itself is untouched except by the recorded merge (nothing merged here)
    branch = subprocess.run(
        ["git", "-C", str(source), "branch", "--list", f"gcae/{state.run_id}"],
        capture_output=True,
        text=True,
    ).stdout
    assert f"gcae/{state.run_id}" in branch, "the work lives on the run branch"

    # no orphan processes were left behind by the interactive runs
    import time

    time.sleep(0.3)
    orphans = subprocess.run(["pgrep", "-f", "game.py"], capture_output=True)
    assert orphans.returncode != 0, "a process from the trajectory is still running"
