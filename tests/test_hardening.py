"""The hardening invariants (A–J) and the mandatory trajectory tests.

Each test asserts one architectural invariant of GCAE as *runtime behavior*, not as
documentation:

A  rejected execution never becomes trusted
B  accepted execution always has relevant evidence
C  rollback restores trusted execution state
D  rollback does not delete learned knowledge
E  context can be reconstructed without previous chat history
F  important pinned information survives context budget pressure
G  the same failed strategy cannot repeat indefinitely
H  final completion requires evidence for every mandatory success criterion
I  the source repository is never destructively manipulated
J  runtime-owned artifacts stay outside the target repository

Trajectory tests 3 (contradictory evidence), 4 (stagnation) and 6 (final evidence
mapping) live here; trajectory tests 1 and 2 are the existing
``test_integration_rollback`` and ``test_adaptive_trajectory`` suites, and test 5 is
``test_context_budget``.
"""

import subprocess
from pathlib import Path

from gcae.context import ContextBuilder, estimate_tokens
from gcae.memory import MemoryStore
from gcae.models import (
    AgentState,
    EvidenceKind,
    EvidenceRecord,
    MemoryRecord,
    SemanticStep,
)
from gcae.planner import LLMPlanner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime, RuntimeControl
from gcae.verifier import FinalVerifier


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-q", "--allow-empty", "-m", "base"],
        check=True,
    )


def fresh_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    return source


def step(action: str, **payload: object) -> dict[str, object]:
    return {
        "action": action,
        "semantic_goal": "goal",
        "reason_summary": payload.pop("reason", "do it"),
        **payload,
    }


def tool(name: str, **arguments: object) -> dict[str, object]:
    """A controller decision that executes one tool (wrapped, as the model would emit)."""
    return {
        "action": "execute_tool",
        "semantic_goal": "goal",
        "reason_summary": f"use {name}",
        "tool": {"name": name, "arguments": arguments},
    }


# ===================================================================== invariant D/E
# The central split: execution state is reversible, knowledge state is cumulative.


def test_rejection_reverts_execution_but_keeps_knowledge(tmp_path: Path) -> None:
    """A rejected candidate disappears from execution state; its lesson and its evidence
    do not disappear from knowledge state (invariants A, C, D)."""
    source = fresh_repo(tmp_path)
    provider = FakeProvider(
        [
            # attempt 1: a broken implementation that fails its own check
            tool("write_file", path="app.py", content="print('broken')\n"),
            tool(
                "run_command",
                command="python -c \"raise SystemExit('boom')\"",
                mode="batch",
            ),
            step("complete_semantic_step"),
            # attempt 2: a different strategy that works
            tool("write_file", path="app.py", content="print('ok')\n"),
            tool("run_command", command="python app.py", mode="batch"),
            step("complete_semantic_step"),
            step("finish_candidate"),
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        # the deterministic gate that rejects attempt 1: the file must contain 'ok'
        validator_commands=["grep -q \"print('ok')\" app.py"],
        control=RuntimeControl(),
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.start("make app.py work", success_criteria=["command succeeds: python app.py"])
    state = runtime.run()

    assert state.status == "complete", state.status
    # A: the broken attempt is not trusted anywhere
    assert runtime.repo is not None
    trusted = runtime.repo.branch_head(state.branch)
    (tmp_path / "checkout").mkdir()
    assert trusted == state.accepted_commit
    # C: the worktree holds the accepted content, not the rejected candidate
    assert "ok" in (Path(state.worktree) / "app.py").read_text()
    assert "broken" not in (Path(state.worktree) / "app.py").read_text()
    # D: the failure lesson and its evidence survived both rollbacks
    assert runtime.memory is not None
    failures = [r for r in runtime.memory.all(state.run_id) if r.kind == "failure"]
    assert any("boom" in r.content for r in failures), "the rejected lesson must survive"
    ledger = runtime.memory.evidence_for_run(state.run_id)
    assert any("boom" in (r.summary + r.source_reference) for r in ledger), (
        "evidence from the rejected attempt must survive"
    )
    # the trajectory tells the whole story from persisted state alone
    assert any(t.status.value == "rejected" for t in state.trajectory)
    assert any(t.status.value == "accepted" for t in state.trajectory)
    # B: the accepted trajectory carries evidence ids
    accepted = [t for t in state.trajectory if t.status.value == "accepted"][0]
    assert accepted.evidence_ids, "accepted work must reference ledger evidence"


def test_context_reconstructs_without_chat_history(tmp_path: Path) -> None:
    """Invariant E: the prompt comes from persisted state, not from a transcript."""
    store = MemoryStore(tmp_path / "memory.db")
    state = AgentState(
        run_id="r", source_repo="/s", worktree="/w", branch="b",
        objective="fix the parser", original_request="fix the parser",
        hard_constraints=["no new dependencies"],
        success_criteria=["file exists: parser.py"],
    )
    store.add(MemoryRecord(kind="user_instruction", content="fix the parser",
                           run_id="r", source_repo="/s", immutable=True))
    store.add(MemoryRecord(kind="failure", content="EOFError: the parser asked for input",
                           run_id="r", source_repo="/s"))
    context = ContextBuilder(store).build(
        state, SemanticStep(id="s1", goal="fix the parser"), budget=2000
    )
    assert "fix the parser" in context.text
    assert "no new dependencies" in context.text
    assert "file exists: parser.py" in context.text
    assert "EOFError" in context.text


def test_pinned_survives_budget_pressure_and_failures_are_bounded(tmp_path: Path) -> None:
    """Invariant F: the request and constraints are lossless; failure records are capped
    so the pinned section cannot grow without bound."""
    store = MemoryStore(tmp_path / "memory.db")
    for index in range(30):
        store.add(MemoryRecord(kind="failure", content=f"failure {index}",
                               run_id="r", source_repo="/s"))
    state = AgentState(
        run_id="r", source_repo="/s", worktree="/w", branch="b",
        objective="objective text", original_request="objective text",
        hard_constraints=["constraint X"], success_criteria=["file exists: out.txt"],
    )
    context = ContextBuilder(store).build(
        state, SemanticStep(id="s1", goal="work"), budget=100
    )
    # P0 information is never dropped, even when the budget is far too small
    for pinned_text in ("objective text", "constraint X", "file exists: out.txt"):
        assert pinned_text in context.text
    assert estimate_tokens(context.text) <= 100 or not context.omitted_ids
    # the store still has every failure: context shortening retrieves fewer, deletes nothing
    assert len(store.all("r")) == 30


def test_source_repository_is_never_touched(tmp_path: Path) -> None:
    """Invariants I and J: the user's checkout stays clean; artifacts live under the
    runtime directory."""
    source = fresh_repo(tmp_path)
    marker = source / "user-work.txt"
    marker.write_text("precious")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-qm", "user work"],
        check=True,
    )
    provider = FakeProvider(
        [
            tool("write_file", path="app.py", content="print('ok')\n"),
            tool("run_command", command="python app.py", mode="batch"),
            step("complete_semantic_step"),
            step("finish_candidate"),
        ]
    )
    runtime = Runtime(source, tmp_path / "runtime", provider=provider,
                      control=RuntimeControl(), auto_merge=False)
    runtime.start("add app.py", success_criteria=["command succeeds: python app.py"])
    state = runtime.run()
    assert state.status == "complete"
    assert marker.read_text() == "precious"
    assert not any(source.rglob("*.pyc")), "no runtime artifacts in the user's tree"
    status = subprocess.run(["git", "-C", str(source), "status", "--porcelain"],
                            capture_output=True, text=True)
    assert not status.stdout.strip(), "the source checkout must stay clean"
    runtime_dir = tmp_path / "runtime"
    assert (runtime_dir / "runs" / state.run_id / "state.json").exists()


# ===================================================================== trajectory 6
# Final completion requires evidence for every mandatory success criterion.


def test_final_verification_requires_evidence_for_every_criterion(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    state = AgentState(
        run_id="r", source_repo="/s", worktree=str(tmp_path), branch="b",
        objective="o", original_request="o",
        success_criteria=["criterion one holds", "criterion two holds", "criterion three holds"],
    )
    judge = FakeProvider(
        [
            {"passed": True, "evidence": "criterion one is supported"},
            {"passed": True, "evidence": "criterion two is supported"},
            {"passed": True, "evidence": "criterion one is supported"},
            {"passed": True, "evidence": "criterion two is supported"},
            {"passed": True, "evidence": "criterion three is supported"},
        ]
    )

    def supporting(criterion: str, record_id: int) -> EvidenceRecord:
        return EvidenceRecord(
            id=record_id, run_id="r", trajectory_step_id="t1",
            kind=EvidenceKind.TEST_RESULT, claim_or_subject=criterion,
            source_type="test", source_reference="test_x",
            summary=f"test proves {criterion}", supports=[criterion],
        )

    report = FinalVerifier(judge).verify(
        state, evidence=[supporting("criterion one holds", 1),
                         supporting("criterion two holds", 2)]
    )
    assert not report.passed, "one criterion has no evidence: completion must be refused"
    unresolved = [c for c in report.criteria if c.status == "insufficient"]
    assert [c.criterion for c in unresolved] == ["criterion three holds"]
    assert judge.calls == 2, "the judge is only consulted where the ledger has evidence"

    # the third criterion gains evidence: completion passes, every criterion maps to it
    report = FinalVerifier(judge).verify(
        state,
        evidence=[
            supporting("criterion one holds", 1),
            supporting("criterion two holds", 2),
            supporting("criterion three holds", 3),
        ],
    )
    assert report.passed
    assert all(c.status == "pass" for c in report.criteria)
    assert all(c.evidence_ids for c in report.criteria)


def test_contradicting_evidence_blocks_an_otherwise_supported_criterion(
    tmp_path: Path,
) -> None:
    """Trajectory test 3, verifier level: a passing check and a contradicting observation
    must both be visible, and the contradiction cannot be silently ignored."""
    (tmp_path / "game.py").write_text("print('bye')\n")
    state = AgentState(
        run_id="r", source_repo="/s", worktree=str(tmp_path), branch="b",
        objective="o", original_request="o",
        success_criteria=["restart functionality works"],
    )
    judge = FakeProvider(
        [{"passed": False, "evidence": "the interactive session exited after 'y'"}]
    )
    evidence = [
        EvidenceRecord(
            id=1, run_id="r", trajectory_step_id="t1", kind=EvidenceKind.TEST_RESULT,
            claim_or_subject="restart functionality works",
            source_type="unit test", source_reference="test_restart",
            summary="test_restart passed", supports=["restart functionality works"],
        ),
        EvidenceRecord(
            id=2, run_id="r", trajectory_step_id="t1", kind=EvidenceKind.INTERACTIVE_SESSION,
            claim_or_subject="restart functionality works",
            source_type="scripted session", source_reference="python game.py",
            summary="process exited after the restart prompt",
            contradicts=["restart functionality works"],
        ),
    ]
    report = FinalVerifier(judge).verify(state, evidence=evidence)
    assert not report.passed, "contradictory evidence must prevent premature acceptance"
    verdict = report.criteria[0]
    assert verdict.status == "fail"
    assert set(verdict.evidence_ids) == {1, 2}, "both sides of the evidence are cited"


# ===================================================================== trajectory 3
# Full runtime trajectory: unit test passes, interactive session contradicts, repair.


def test_contradictory_evidence_forces_repair_before_completion(tmp_path: Path) -> None:
    """The unit test passes but the scripted session hits the declared failure signal;
    the run must not complete until the contradiction is gone (trajectory test 3)."""
    source = fresh_repo(tmp_path)
    game = '''import sys


def should_restart(answer: str) -> bool:
    return answer.strip().lower() == "y"


if __name__ == "__main__":
    answer = input("play again? ")
    if should_restart(answer):
        print("exiting after restart prompt")
    else:
        print("bye")
'''
    provider = FakeProvider(
        [
            # step 1: write the game, its unit test passes, but the session contradicts
            tool("write_file", path="game.py", content=game),
            tool("write_file", path="test_game.py",
                 content="from game import should_restart\\n"
                         "assert should_restart('y')\\n"),
            tool("run_command", command="python test_game.py", mode="batch"),
            tool("run_command", command="python game.py", mode="scripted_input",
                 stdin=["y"]),
            step("complete_semantic_step"),
            # the plan is empty now: the run verifies automatically, the judge sees both
            # sides of the evidence and refuses, so the run reopens with a repair step
            tool("write_file", path="game.py",
                 content=game.replace(
                     "print(\"exiting after restart prompt\")",
                     "print(\"a new game starts\")",
                 )),
            tool("run_command", command="python game.py", mode="scripted_input",
                 stdin=["y"]),
            step("complete_semantic_step"),
        ]
    )
    judge = FakeProvider(
        [
            {"passed": False, "evidence": "the session exited after the restart prompt"},
            {"passed": True, "evidence": "the scripted session starts a new game"},
        ]
    )
    planner = LLMPlanner(
        FakeProvider(
            [
                {
                    "objective": "make restart work",
                    "success_criteria": ["restart functionality works"],
                    "hard_constraints": [],
                    "assumptions": [],
                    "steps": [
                        {
                            "id": "step-1",
                            "goal": "make restart work",
                            "rationale": "the restart path exits instead of looping",
                            "expected_result": "restart functionality works",
                            "expected_evidence": ["the unit test passes",
                                                 "the scripted session restarts"],
                            "failure_signals": ["exiting after restart prompt"],
                            "intended_scope": ["game.py", "test_game.py"],
                            "validation_requirements": ["restart functionality works"],
                        }
                    ],
                }
            ]
        )
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        planner=planner,
        verifier=FinalVerifier(judge),
        control=RuntimeControl(),
        auto_merge=False,
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.start("make restart work", success_criteria=["restart functionality works"])
    state = runtime.run()

    assert state.status == "complete", state.status
    assert runtime.memory is not None
    ledger = runtime.memory.evidence_for_run(state.run_id)
    contradicting = [r for r in ledger if r.contradicts]
    assert contradicting, "the contradictory observation must be in the ledger"
    assert any("exiting after restart prompt" in r.summary for r in contradicting)
    # the repaired program no longer prints the failure signal
    assert "a new game starts" in (Path(state.worktree) / "game.py").read_text()
    # the refusal is visible in the ledger (criterion fail, above) and the run only
    # completed through a second trajectory attempt: the repaired step
    parents = [t.parent_plan_step_id for t in state.trajectory if t.status.value == "accepted"]
    assert parents == ["step-1", "step-2"], "the repair must be its own accepted trajectory"
    assert state.last_verification is not None and state.last_verification.passed
    assert any(e.event_type == "trajectory_step_completed" for e in events)


# ===================================================================== trajectory 4
# Stagnation: the same failing approach is refused and strategy reconsideration follows.


def test_repeated_failing_strategy_is_refused_and_reconsidered(tmp_path: Path) -> None:
    """Trajectory test 4: identical failing commands are refused without executing,
    the failed strategy is remembered, and stagnation triggers a correction."""
    source = fresh_repo(tmp_path)
    provider = FakeProvider(
        [
            # the same broken command, twice, then refused, then again: the run never
            # changes the file, so the strategy ledger sees an unchanged candidate tree
            tool("run_command", command="python missing_module.py", mode="batch"),
            tool("run_command", command="python missing_module.py", mode="batch"),
            step("complete_semantic_step"),
            tool("run_command", command="python missing_module.py", mode="batch"),
            step("complete_semantic_step"),
            tool("run_command", command="python missing_module.py", mode="batch"),
            step("complete_semantic_step"),
            # the advisor reads the trace and prescribes a different method
            {
                "root_cause": "the same missing-module command keeps failing",
                "corrective_instruction": "create missing_module.py first",
                "strategy": "replan",
            },
            tool("write_file", path="missing_module.py", content="print('here')\n"),
            tool("run_command", command="python missing_module.py", mode="batch"),
            step("complete_semantic_step"),
            step("finish_candidate"),
        ]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        # rejection gate: the module must exist before the step can be accepted
        validator_commands=["test -f missing_module.py"],
        control=RuntimeControl(),
        auto_merge=False,
    )
    events: list = []
    runtime.subscribe(events.append)
    runtime.start("run missing_module.py", success_criteria=["file exists: missing_module.py"])
    state = runtime.run()

    assert state.status == "complete", state.status
    kinds = [e.event_type for e in events]
    assert "failure_classified" in kinds, "the failure must be interpreted and remembered"
    assert "strategy_ineffective" in kinds, "the identical retry must be refused"
    assert "recovery_started" in kinds, "stagnation must trigger strategy reconsideration"
    assert runtime.memory is not None
    lessons = [r.content for r in runtime.memory.all(state.run_id) if r.kind == "failure"]
    assert any("missing_module" in lesson for lesson in lessons)
    refused = [
        e for e in events
        if e.event_type == "strategy_ineffective"
    ]
    assert refused, "the identical attempt must be refused before execution"
    # the failed strategy is recorded as a trajectory, and the final strategy differs
    assert any(t.status.value in {"rejected", "replanned"} for t in state.trajectory)
    assert "here" in (Path(state.worktree) / "missing_module.py").read_text()


# ===================================================================== evidence ledger


def test_evidence_ledger_scopes_by_run_and_step(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    first = store.add_evidence(
        EvidenceRecord(run_id="run-1", trajectory_step_id="t-1-1",
                       kind=EvidenceKind.COMMAND_RESULT, claim_or_subject="claim a",
                       summary="exit 0")
    )
    store.add_evidence(
        EvidenceRecord(run_id="run-2", trajectory_step_id="t-2-1",
                       kind=EvidenceKind.COMMAND_RESULT, claim_or_subject="claim b",
                       summary="exit 1", contradicts=["claim b"])
    )
    assert first.id == 1
    assert len(store.evidence_for_run("run-1")) == 1
    assert len(store.evidence_for_run("run-2")) == 1
    assert len(store.evidence_for_step("t-2-1")) == 1
    assert store.evidence_for_run("run-2")[0].contradicts == ["claim b"]


def test_memory_retrieval_is_scoped_to_the_repository(tmp_path: Path) -> None:
    """A lesson learned in repository A must not enter repository B's context."""
    store = MemoryStore(tmp_path / "memory.db")
    store.add(MemoryRecord(kind="failure", content="'python game.py' asks for input",
                           run_id="run-A", source_repo="/repo/A"))
    store.add(MemoryRecord(kind="failure", content="'python game.py' asks for input",
                           run_id="run-B", source_repo="/repo/B"))
    for_project_b = store.search("game input", limit=10, source_repo="/repo/B")
    assert [r.record.run_id for r in for_project_b] == ["run-B"], (
        "another repository's failure lesson leaked into this project's context"
    )
    for_project_a = store.search("game input", limit=10, source_repo="/repo/A")
    assert [r.record.run_id for r in for_project_a] == ["run-A"]


def test_legacy_memory_database_gains_the_source_repo_column(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "memory.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            content TEXT NOT NULL,
            run_id TEXT NOT NULL,
            step_id TEXT,
            source TEXT NOT NULL,
            commit_sha TEXT,
            created_at TEXT NOT NULL,
            importance INTEGER NOT NULL,
            immutable INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE memory_fts USING fts5(
            content, kind, content='memory', content_rowid='id'
        );
        """
    )
    connection.commit()
    connection.close()
    store = MemoryStore(path)  # opens the pre-0.4.0 schema and migrates it
    saved = store.add(MemoryRecord(kind="fact", content="still works", run_id="r",
                                   source_repo="/s"))
    assert saved.id == 1
    assert [r.record.run_id for r in store.search("works", source_repo="/s")] == ["r"]


def test_backfill_attributes_legacy_rows_to_their_repository(tmp_path: Path) -> None:
    """Pre-0.4.0 rows carry an empty source_repo and are invisible to scoped search;
    backfilling from the run state files makes them retrievable without rewriting
    rows that already carry a repository."""
    store = MemoryStore(tmp_path / "memory.db")
    store.add(MemoryRecord(kind="failure", content="old lesson about games",
                           run_id="run-old", source_repo=""))
    store.add(MemoryRecord(kind="failure", content="already scoped lesson",
                           run_id="run-old", source_repo="/repo/other"))
    updated = store.backfill_source_repos({"run-old": "/repo/A", "run-x": "", "": "/repo/A"})
    assert updated == 1, "only the blank row may be rewritten"
    assert [r.record.run_id for r in store.search("games", source_repo="/repo/A")] == ["run-old"]
    scoped = store.search("scoped lesson", source_repo="/repo/other")
    assert [r.record.run_id for r in scoped] == ["run-old"]


def test_runtime_backfills_legacy_memory_on_start(tmp_path: Path) -> None:
    """Opening a run backfills legacy rows from the persisted run states (best effort)."""
    from gcae.persistence import StateStore

    source = fresh_repo(tmp_path)
    runtime_dir = tmp_path / "runtime"
    store = MemoryStore(runtime_dir / "memory.db")
    store.add(MemoryRecord(kind="failure", content="legacy lesson token xyzzy",
                           run_id="legacy-run", source_repo=""))
    legacy = AgentState(run_id="legacy-run", source_repo=str(source), worktree="/w",
                        branch="b", objective="o", original_request="o")
    StateStore(runtime_dir / "runs" / "legacy-run" / "state.json").save(legacy)
    runtime = Runtime(source, runtime_dir, control=RuntimeControl())
    runtime.memory = MemoryStore(runtime_dir / "memory.db")
    runtime._backfill_memory_repos()
    assert runtime.memory is not None
    found = runtime.memory.search("xyzzy", source_repo=str(source))
    assert [r.record.run_id for r in found] == ["legacy-run"]
