"""Developer-only visual QA harness for the dashboard.

Replays a scripted, realistic event sequence through a real ``Runtime`` (temp repo,
deterministic fake provider) so the whole interface can be inspected without spending
model tokens: running state, candidate changes, validation failure, rollback, replan,
accepted checkpoint, pause and completion.

    python tools/tui_demo.py                                  # interactive dashboard
    python tools/tui_demo.py --plain --size 150x46             # final frame as text
    python tools/tui_demo.py --plain --size 80x28 --capture 12  # mid-run frame

The `--frames` mode exists to verify what the tests cannot: that the *rendered* screen stays
put while model telemetry streams in.  It prints consecutive frames plus the geometry of every
panel (x, y, width, height, body rows), so a reflow or a jumping box is visible as a diff.

    python tools/tui_demo.py --plain --size 160x45 --capture 3.3 --frames 5 --interval 0.15

Not imported by any product code.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from gcae.models import (
    RunPhase,  # noqa: E402
)
from gcae.planner import LLMPlanner  # noqa: E402
from gcae.providers import FakeProvider  # noqa: E402
from gcae.runtime import Runtime, RuntimeControl  # noqa: E402
from gcae.tools import ToolRegistry  # noqa: E402
from gcae.tui.app import GcaeApp  # noqa: E402
from gcae.validation import DeterministicValidator  # noqa: E402

PLAN = {
    "objective": "fix the streaming CSV parser without changing the public API",
    "success_criteria": ["command succeeds: pytest -q tests/test_parser.py"],
    "hard_constraints": ["do not change the public API"],
    "assumptions": ["the parser is the only consumer of quote state"],
    "steps": [
        {
            "id": "step-1",
            "goal": "inspect the parser architecture",
            "rationale": "understand chunk handling before touching it",
            "expected_result": "known entry points",
            "intended_scope": ["src/parser.py"],
            "validation_requirements": [],
        },
        {
            "id": "step-2",
            "goal": "reproduce the chunk boundary failure",
            "rationale": "confirm the failure mode",
            "expected_result": "failing test",
            "intended_scope": ["tests/test_parser.py"],
            "validation_requirements": [],
        },
        {
            "id": "step-3",
            "goal": "fix quote state across chunks",
            "rationale": "root cause",
            "expected_result": "quotes survive boundaries",
            "intended_scope": ["src/parser.py"],
            "validation_requirements": ["command succeeds: pytest -q tests/test_parser.py"],
        },
        {
            "id": "step-4",
            "goal": "run the regression tests",
            "rationale": "no regressions",
            "expected_result": "suite green",
            "intended_scope": [],
            "validation_requirements": [],
        },
        {
            "id": "step-5",
            "goal": "final verification",
            "rationale": "criteria met",
            "expected_result": "verified",
            "intended_scope": [],
            "validation_requirements": [],
        },
    ],
}

TRAJECTORY = [
    {
        "action": "execute_tool",
        "semantic_goal": "read the parser",
        "reason_summary": "read the entry points",
        "expected_result": "chunk handling located",
        "tool": {"name": "read_file", "arguments": {"path": "README"}},
    },
    {"action": "complete_semantic_step", "semantic_goal": "inspect", "reason_summary": "done"},
    {
        "action": "execute_tool",
        "semantic_goal": "reproduce",
        "reason_summary": "write a failing test",
        "tool": {
            "name": "create_file",
            "arguments": {"path": "tests/test_parser.py", "content": "def test_boundary(): ..."},
        },
    },
    {"action": "complete_semantic_step", "semantic_goal": "reproduce", "reason_summary": "done"},
]


def demo_repo(root: Path) -> Path:
    repo = root / "parser-project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "demo@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Demo"], check=True)
    (repo / "README").write_text("streaming csv parser\n")
    (repo / "src").mkdir()
    (repo / "src" / "parser.py").write_text("def feed(chunk):\n    return chunk\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
    return repo


def script(runtime: Runtime, stop: threading.Event) -> None:
    """Replay a realistic sequence of events, using real git and validation data.

    Even the demos that look scripted are real: candidate files are written to the
    worktree, validation commands actually run, the rollback is a real ``git reset``
    and the checkpoint is a real commit, so every number on screen is genuine.
    """
    state = runtime.state
    repo = runtime.repo
    assert state is not None and repo is not None and repo.worktree is not None
    tools = ToolRegistry(repo.worktree)
    worktree = repo.worktree

    def emit(
        kind: str,
        payload: dict[str, object] | None = None,
        phase: RunPhase | None = None,
        step: str | None = None,
        pause: float = 0.8,
    ) -> None:
        runtime._event(kind, phase, step_id=step, payload=payload or {})
        time.sleep(pause)

    def model_call(role: str, model: str, *, chunks: int = 4, characters: int = 420) -> None:
        """Emit the streaming telemetry a real provider produces for one call."""
        started = time.monotonic()
        runtime._event(
            "provider_started",
            None,
            payload={"role": role, "model": model},
        )
        time.sleep(0.2)
        runtime._event(
            "provider_first_token",
            None,
            payload={"role": role, "model": model, "elapsed_ms": 200.0, "characters": 1},
        )
        for index in range(1, chunks + 1):
            time.sleep(0.15)
            runtime._event(
                "provider_progress",
                None,
                payload={
                    "role": role,
                    "model": model,
                    "characters": index * characters,
                    "reasoning_characters": index * 90,
                    "elapsed_ms": (time.monotonic() - started) * 1000,
                    "preview": "partial generation that must never reach the main screen",
                },
            )
        runtime._event(
            "provider_finished",
            None,
            payload={
                "role": role,
                "model": model,
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "characters": chunks * characters,
            },
        )

    def plan_payload(reason: str, completed: int, **extra: object) -> dict[str, object]:
        payload: dict[str, object] = {
            "reason": reason,
            "steps": [
                {"id": step.id, "goal": step.goal, "status": step.status} for step in state.plan
            ],
            "completed": completed,
        }
        payload.update(extra)
        return payload

    runtime._transition(RunPhase.PLAN)
    emit("plan_updated", plan_payload("initial plan", 0), RunPhase.PLAN)
    runtime._transition(RunPhase.EXECUTE)
    emit(
        "step_started",
        {"goal": state.plan[0].goal, "index": 1, "total": len(state.plan)},
        RunPhase.EXECUTE,
        "step-1",
    )
    emit(
        "context_built",
        {"characters": 31200, "estimated_tokens": 7800, "pinned": 6, "omitted": 1},
        RunPhase.EXECUTE,
        "step-1",
    )
    emit("memory_updated", {"counts": {"user_instruction": 3, "fact": 4, "decision": 2}})
    model_call("controller", "qwen/qwen3-coder")
    emit(
        "decision",
        {
            "action": "execute_tool",
            "reason_summary": "read the parser entry points",
            "expected_result": "chunk handling located",
            "tool": {"name": "read_file", "arguments": {"path": "src/parser.py", "limit": 400}},
        },
        RunPhase.EXECUTE,
        "step-1",
    )
    emit(
        "tool_result",
        {
            "tool": "read_file",
            "success": True,
            "duration_ms": 320.0,
            "output": "def feed(chunk): ...",
        },
        RunPhase.EXECUTE,
        "step-1",
    )

    # real candidate work: a modified file and a new file
    (worktree / "src" / "parser.py").write_text(
        "def feed(chunk, *, strict=False):\n    return chunk\n"
    )
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_parser.py").write_text("def test_boundary():\n    assert True\n")
    emit("candidate_state", repo.candidate_snapshot(), RunPhase.EXECUTE, "step-1")

    # real failing validation (the command genuinely exits non-zero)
    validator = DeterministicValidator(repo, tools, ['python -c "import sys; sys.exit(1)"'])
    runtime._transition(RunPhase.VALIDATE)
    validation = validator.validate(["src/parser.py"])
    state.latest_validation = validation
    emit(
        "validation",
        validation.model_dump(mode="json"),
        RunPhase.VALIDATE,
        "step-1",
    )
    emit(
        "evaluation",
        {
            "decision": "rollback",
            "reason": "candidate modified the public parser signature unnecessarily",
            "next_goal": "fix quote state without touching the public API",
            "memories_to_promote": [
                {
                    "record": {
                        "kind": "decision",
                        "content": "keep the public parser signature stable",
                    }
                }
            ],
        },
        RunPhase.EVALUATE,
        "step-1",
    )

    # real rollback of the speculative candidate
    runtime._transition(RunPhase.ROLLBACK)
    discarded = [entry["path"] for entry in repo.candidate_snapshot()["files"]]
    target = state.accepted_commit or repo.current_commit()
    repo.rollback(target)
    emit(
        "rollback_completed",
        {
            "from_commit": target,
            "to_commit": target,
            "reason": "candidate modified the public parser signature unnecessarily",
            "discarded": discarded,
        },
        RunPhase.ROLLBACK,
        "step-1",
    )
    emit("candidate_state", repo.candidate_snapshot(), RunPhase.ROLLBACK, "step-1")

    # replan: two steps were completed, the failed step is replaced
    state.accepted_steps = 2
    state.plan = [step for step in state.plan if step.id not in {"step-1", "step-2"}]
    state.plan[0].status = "active"
    runtime._transition(RunPhase.PLAN)
    emit(
        "plan_updated",
        plan_payload(
            "candidate modified the public parser signature unnecessarily",
            2,
            replaced="step-1",
            failed=True,
        ),
        RunPhase.PLAN,
    )
    runtime._transition(RunPhase.EXECUTE)
    emit(
        "step_started",
        {"goal": state.plan[0].goal, "index": 3, "total": 5},
        RunPhase.EXECUTE,
        state.plan[0].id,
    )
    emit(
        "decision",
        {
            "action": "execute_tool",
            "reason_summary": "run the boundary tests",
            "expected_result": "quotes survive chunk boundaries",
            "tool": {
                "name": "run_command",
                "arguments": {"command": "pytest -q tests/test_parser.py"},
            },
        },
        RunPhase.EXECUTE,
        state.plan[0].id,
    )

    # real acceptance: the fix (and its test) is committed as a checkpoint
    (worktree / "tests").mkdir(exist_ok=True)
    (worktree / "tests" / "test_parser.py").write_text(
        "def test_quotes_survive_chunk_boundaries():\n    assert True\n"
    )
    (worktree / "src" / "parser.py").write_text(
        "def feed(chunk):\n    return chunk  # quote state preserved across chunks\n"
    )
    # the runtime always cleans generated artifacts before validating/verifying
    repo.clean_generated_artifacts()
    repo.clean_ignored_artifacts()
    runtime._transition(RunPhase.VALIDATE)
    passing = DeterministicValidator(repo, tools, ["python -c \"print('12 passed')\""]).validate(
        ["src/parser.py", "tests/test_parser.py"]
    )
    state.latest_validation = passing
    emit("validation", passing.model_dump(mode="json"), RunPhase.VALIDATE, state.plan[0].id)
    runtime._transition(RunPhase.EVALUATE)
    runtime._transition(RunPhase.CHECKPOINT)
    commit = repo.checkpoint(f"gcae: {state.plan[0].goal}")
    state.accepted_commit = commit
    step_id = state.plan[0].id
    step_goal = state.plan[0].goal
    state.accepted_steps = 3
    state.plan = [step for step in state.plan if step.id != step_id]
    emit(
        "checkpoint_created",
        {"commit": commit, "message": f"gcae: {step_goal}", "kind": "step"},
        RunPhase.CHECKPOINT,
        step_id,
    )
    emit(
        "candidate_state",
        repo.candidate_snapshot(),
        RunPhase.CHECKPOINT,
        step_id,
    )
    # leave a small real candidate so the DIRTY state is visible
    (worktree / "src" / "parser.py").write_text(
        "def feed(chunk):\n    return chunk  # quote state preserved across chunks\n"
        "# follow-up tweak\n"
    )
    emit(
        "evaluation",
        {
            "decision": "accept",
            "reason": "boundary tests pass and the public API is unchanged",
            "next_goal": None,
            "memories_to_promote": [],
        },
        RunPhase.EVALUATE,
        step_id,
    )
    emit(
        "step_accepted",
        {
            "goal": step_goal,
            "commit": commit,
            "changed_files": ["src/parser.py", "tests/test_parser.py"],
            "accepted_steps": 3,
            "remaining_steps": len(state.plan),
        },
        RunPhase.CHECKPOINT,
        step_id,
    )
    emit("candidate_state", repo.candidate_snapshot(), RunPhase.CHECKPOINT, step_id)

    repo.clean_generated_artifacts()
    repo.clean_ignored_artifacts()
    runtime._transition(RunPhase.VERIFY)
    emit("verification_started", None, RunPhase.VERIFY)
    report = runtime.verifier.verify(state, diff=repo.diff())
    state.last_verification = report
    emit("verification_completed", report.model_dump(mode="json"), RunPhase.VERIFY)

    state.status = "complete"
    state.plan = []
    runtime._transition(RunPhase.COMPLETE)
    emit("run_completed", None, RunPhase.COMPLETE, pause=0.2)
    stop.set()


PANEL_IDS = (
    "status",
    "objective",
    "plan",
    "activity",
    "checkpoint",
    "validation",
    "evaluation",
    "banner",
    "metrics",
    "timeline",
    "footer",
)

#: Text that must never reach the main screen: model telemetry, not run state.
TELEMETRY_MARKERS = ("chars", "streaming", "first token", "preview", "reasoning")


def print_frame(app: GcaeApp, size: tuple[int, int]) -> None:
    strips = app.screen._compositor.render_strips()
    print("=== full frame " + "=" * (size[0] - 17))
    for strip in strips:
        print(strip.text.rstrip())
    print()


def print_geometry(app: GcaeApp) -> None:
    """Panel geometry + a telemetry scan: the machine-readable part of a visual check."""
    print("--- geometry " + "-" * 60)
    for panel_id in PANEL_IDS:
        try:
            widget = app.query_one(f"#{panel_id}")
        except Exception:  # noqa: BLE001 - a panel may be absent at this size
            continue
        body = getattr(widget, "body", None)
        rows = len(body.plain.splitlines()) if body is not None else 0
        region = widget.region
        hidden = "" if widget.display else " hidden"
        print(
            f"{panel_id:11} x={region.x:3} y={region.y:2} w={region.width:3} "
            f"h={region.height:2} rows={rows:2}{hidden}"
        )
    timeline = str(app.query_one("#timeline").body.plain)
    activity = str(app.query_one("#activity").body.plain)
    leaked_timeline = [m for m in TELEMETRY_MARKERS if m in timeline]
    leaked_activity = [m for m in TELEMETRY_MARKERS if m in activity]
    print(f"timeline telemetry={leaked_timeline or 'none'}")
    print(f"activity telemetry={leaked_activity or 'none'}")
    print()


def plain_render(
    size: tuple[int, int], capture: float = 0.0, frames: int = 1, interval: float = 0.25
) -> None:
    """Render frames at a given size and print them (text only) with panel geometry."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = demo_repo(root)
        runtime = Runtime(
            repo,
            root / "state",
            provider=FakeProvider(
                [{"action": "finish_candidate", "semantic_goal": "x", "reason_summary": "x"}]
            ),
            planner=LLMPlanner(FakeProvider([PLAN])),
            control=RuntimeControl(),
            provider_label="fake",
        )
        runtime.start("fix the streaming CSV parser", success_criteria=["file exists: README"])
        app = GcaeApp(runtime, auto_run=False)

        async def scenario() -> None:
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                thread_stop = threading.Event()
                worker = threading.Thread(target=script, args=(runtime, thread_stop), daemon=True)
                worker.start()
                if capture:
                    await asyncio.sleep(capture)
                else:
                    while not thread_stop.is_set():
                        await pilot.pause(0.2)
                    await pilot.pause(1.5)
                for index in range(max(1, frames)):
                    if index:
                        await asyncio.sleep(interval)
                    print(f"### frame {index + 1}/{max(1, frames)}")
                    print_frame(app, size)
                    print_geometry(app)

        asyncio.run(scenario())


def main() -> None:
    parser = argparse.ArgumentParser(description="GCAE dashboard demo")
    parser.add_argument("--plain", action="store_true", help="render one frame and exit")
    parser.add_argument("--size", default="140x45", help="plain render size, e.g. 140x45")
    parser.add_argument(
        "--capture",
        type=float,
        default=0.0,
        help="capture the frame this many seconds into the run (default: final frame)",
    )
    parser.add_argument(
        "--frames", type=int, default=1, help="print this many consecutive frames"
    )
    parser.add_argument(
        "--interval", type=float, default=0.25, help="seconds between printed frames"
    )
    args = parser.parse_args()
    if args.plain:
        width, height = (int(part) for part in args.size.split("x"))
        plain_render(
            (width, height), capture=args.capture, frames=args.frames, interval=args.interval
        )
        return
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        repo = demo_repo(root)
        runtime = Runtime(
            repo,
            root / "state",
            provider=FakeProvider(
                [{"action": "finish_candidate", "semantic_goal": "x", "reason_summary": "x"}]
            ),
            planner=LLMPlanner(FakeProvider([PLAN])),
            control=RuntimeControl(),
            provider_label="fake",
        )
        runtime.start("fix the streaming CSV parser", success_criteria=["file exists: README"])
        app = GcaeApp(runtime, auto_run=False)
        stop = threading.Event()
        threading.Thread(target=script, args=(runtime, stop), daemon=True).start()
        app.run()


if __name__ == "__main__":
    main()
