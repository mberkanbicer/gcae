from pathlib import Path

from gcae.models import AgentState, ValidationResult
from gcae.providers import FakeProvider
from gcae.verifier import FinalVerifier


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


def test_hybrid_verifier_judges_unsupported_criterion(tmp_path: Path) -> None:
    (tmp_path / "answer.txt").write_text("correct")
    judge = FakeProvider(
        [{"passed": True, "evidence": "answer.txt contains the expected output"}]
    )
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, "the implementation is correct")
    )
    assert report.passed
    assert report.criteria[0].evidence == "answer.txt contains the expected output"


def test_hybrid_verifier_requires_evidence(tmp_path: Path) -> None:
    judge = FakeProvider([{"passed": True, "evidence": "   "}])
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, "the implementation is correct")
    )
    assert not report.passed
    assert "no evidence" in report.criteria[0].evidence


def test_hybrid_verifier_fails_closed_on_provider_error(tmp_path: Path) -> None:
    judge = FakeProvider([], repair_limit=0)
    report = FinalVerifier(judge).verify(
        make_state(tmp_path, "the implementation is correct")
    )
    assert not report.passed
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
    state = make_state(tmp_path, "note.txt contains the requested text")
    state.latest_validation = ValidationResult(
        passed=True,
        changed_files=["note.txt"],
        new_files=["note.txt"],
    )
    capture = PromptCapture(
        {"passed": True, "evidence": "note.txt contains 'hybrid ok'"}
    )
    report = FinalVerifier(capture).verify(state)
    assert report.passed
    assert "hybrid ok" in capture.prompts[0]
    assert "worktree_samples" in capture.prompts[0]
