from pathlib import Path

from gcae.models import AgentState, EvidenceKind, EvidenceRecord, ValidationResult
from gcae.providers import FakeProvider
from gcae.verifier import FinalVerifier


def supporting(criterion: str) -> EvidenceRecord:
    """One ledger record speaking for a criterion, as the runtime would have written it."""
    # the verifier always receives ledger records, which carry their ids
    return EvidenceRecord(
        id=1,
        run_id="r",
        trajectory_step_id="trajectory-step-1-1",
        kind=EvidenceKind.TEST_RESULT,
        claim_or_subject=criterion,
        source_type="test",
        source_reference="test_restart",
        summary="test_restart passed",
        supports=[criterion],
    )


def test_verifier_checks_file_content_and_command(tmp_path: Path) -> None:
    (tmp_path / "answer.txt").write_text("correct")
    state = AgentState(
        run_id="r",
        source_repo="/source",
        worktree=str(tmp_path),
        branch="b",
        objective="o",
        original_request="o",
        success_criteria=[
            "file exists: answer.txt",
            "file contains: answer.txt :: correct",
            "command succeeds: test -f answer.txt",
        ],
    )
    report = FinalVerifier().verify(state)
    assert report.passed


def test_verifier_rejects_unsupported_criterion(tmp_path: Path) -> None:
    state = AgentState(
        run_id="r",
        source_repo="/source",
        worktree=str(tmp_path),
        branch="b",
        objective="o",
        original_request="o",
        success_criteria=["the implementation is correct"],
    )
    report = FinalVerifier().verify(state)
    assert not report.passed
    assert report.missing_requirements == ["the implementation is correct"]
    assert "unsupported criterion" in report.criteria[0].evidence


def make_state(tmp_path: Path, criterion: str) -> AgentState:
    return AgentState(
        run_id="r",
        source_repo="/source",
        worktree=str(tmp_path),
        branch="b",
        objective="o",
        original_request="o",
        success_criteria=[criterion],
    )


def test_exact_content_criterion(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hybrid ok")
    assert FinalVerifier().verify(
        make_state(tmp_path, "file contains exactly: note.txt :: hybrid ok")
    ).passed
    # a trailing newline at end of file is conventional, not a content difference
    (tmp_path / "note.txt").write_text("hybrid ok\n")
    assert FinalVerifier().verify(
        make_state(tmp_path, "file contains exactly: note.txt :: hybrid ok")
    ).passed
    # anything beyond the trailing newline is still a failure
    (tmp_path / "note.txt").write_text("hybrid ok\nextra\n")
    report = FinalVerifier().verify(
        make_state(tmp_path, "file contains exactly: note.txt :: hybrid ok")
    )
    assert not report.passed
    assert "found 'hybrid ok\\nextra\\n'" in report.criteria[0].evidence


def test_hybrid_verifier_judges_unsupported_criterion(tmp_path: Path) -> None:
    (tmp_path / "answer.txt").write_text("correct")
    judge = FakeProvider(
        [{"passed": True, "evidence": "answer.txt contains the expected output"}]
    )
    criterion = "the implementation is correct"
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, criterion), evidence=[supporting(criterion)]
    )
    assert report.passed
    assert report.criteria[0].evidence == "answer.txt contains the expected output"
    assert report.criteria[0].status == "pass"
    assert report.criteria[0].evidence_ids == [1]


def test_hybrid_verifier_requires_evidence(tmp_path: Path) -> None:
    """A criterion nothing in the ledger speaks to is INSUFFICIENT, not judged."""
    judge = FakeProvider([{"passed": True, "evidence": "   "}])
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, "the implementation is correct")
    )
    assert not report.passed
    assert report.criteria[0].status == "insufficient"
    assert "no evidence in the ledger" in report.criteria[0].evidence
    assert judge.calls == 0, "the judge must not be asked to invent evidence"

    # with ledger evidence, a blank citation still fails closed
    judge = FakeProvider([{"passed": True, "evidence": "   "}])
    criterion = "the implementation is correct"
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, criterion), evidence=[supporting(criterion)]
    )
    assert not report.passed
    assert report.criteria[0].status == "fail"
    assert "no evidence" in report.criteria[0].evidence


def test_hybrid_verifier_fails_closed_on_provider_error(tmp_path: Path) -> None:
    judge = FakeProvider([], repair_limit=0)
    criterion = "the implementation is correct"
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, criterion), evidence=[supporting(criterion)]
    )
    assert not report.passed
    assert report.criteria[0].status == "fail"
    assert "judge unavailable" in report.criteria[0].evidence


def test_checkable_criteria_never_call_the_judge(tmp_path: Path) -> None:
    (tmp_path / "answer.txt").write_text("correct")
    judge = FakeProvider([], repair_limit=0)
    report = FinalVerifier(judge).verify(make_state(tmp_path, "file exists: answer.txt"))
    assert report.passed


class PromptCapture:
    def __init__(self, judgement: dict[str, object]) -> None:
        self.judgement = judgement
        self.prompts: list[str] = []

    def complete(self, prompt, schema):  # type: ignore[no-untyped-def]
        self.prompts.append(prompt)
        return schema.model_validate(self.judgement)


def test_hybrid_verifier_receives_worktree_samples(tmp_path: Path) -> None:
    (tmp_path / "note.txt").write_text("hybrid ok")
    criterion = "note.txt contains the requested text"
    state = make_state(tmp_path, criterion)
    state.latest_validation = ValidationResult(
        passed=True,
        changed_files=["note.txt"],
        new_files=["note.txt"],
    )
    capture = PromptCapture(
        {"passed": True, "evidence": "note.txt contains 'hybrid ok'"}
    )
    report = FinalVerifier(capture).verify(state, evidence=[supporting(criterion)])
    assert report.passed
    assert "hybrid ok" in capture.prompts[0]
    assert "worktree_samples" in capture.prompts[0]
    assert "ledger_evidence" in capture.prompts[0]


def test_hygiene_is_measured_before_criterion_commands_run(tmp_path: Path) -> None:
    """A criterion that runs pytest generates __pycache__; that must not fail hygiene."""
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    state = AgentState(
        run_id="r",
        source_repo="/source",
        worktree=str(tmp_path),
        branch="b",
        objective="o",
        original_request="o",
        success_criteria=["command succeeds: python -m pytest -q tests/test_ok.py"],
    )
    report = FinalVerifier().verify(state)
    assert report.criteria[0].passed
    assert report.hygiene_passed
    # the criterion really did run pytest and really did create caches
    assert list((tmp_path / "tests").rglob("*.pyc"))


def test_judge_sees_newest_evidence_first(tmp_path: Path) -> None:
    """Recency is explicit, not scored: the bundle is newest-first with timestamps, so a
    repaired run's fresh support outranks the stale contradiction it replaced."""
    from datetime import UTC, datetime

    criterion = "restart functionality works"
    stale = EvidenceRecord(
        id=1, run_id="r", trajectory_step_id="t1", kind=EvidenceKind.OBSERVATION,
        claim_or_subject=criterion, source_type="session", source_reference="game.py",
        summary="old contradiction", contradicts=[criterion],
        created_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    fresh = EvidenceRecord(
        id=2, run_id="r", trajectory_step_id="t2", kind=EvidenceKind.TEST_RESULT,
        claim_or_subject=criterion, source_type="test", source_reference="test_restart",
        summary="fresh support", supports=[criterion],
    )
    capture = PromptCapture({"passed": True, "evidence": "the fresh session restarts"})
    report = FinalVerifier(capture).verify(
        make_state(tmp_path, criterion), evidence=[stale, fresh]
    )
    assert report.passed
    prompt = capture.prompts[0]
    assert "newest first" in prompt
    assert prompt.index('"id":2') < prompt.index('"id":1'), "newest cited first"
    assert '"created_at"' in prompt
    assert report.criteria[0].evidence_ids == [2]


def test_contradiction_cites_newest_first(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    criterion = "restart functionality works"

    def against(record_id: int, year: int, summary: str) -> EvidenceRecord:
        return EvidenceRecord(
            id=record_id, run_id="r", trajectory_step_id="t1",
            kind=EvidenceKind.OBSERVATION, claim_or_subject=criterion,
            source_type="session", source_reference="game.py", summary=summary,
            contradicts=[criterion],
            created_at=datetime(year, 1, 1, tzinfo=UTC),
        )

    report = FinalVerifier(FakeProvider([], repair_limit=0)).verify(
        make_state(tmp_path, criterion),
        evidence=[against(1, 2020, "old contradiction"), against(2, 2024, "new contradiction")],
    )
    assert not report.passed
    assert report.criteria[0].status == "fail"
    assert report.criteria[0].evidence.index("E2") < report.criteria[0].evidence.index("E1")
