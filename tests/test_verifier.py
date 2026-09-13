from pathlib import Path

from gcae.models import AgentState
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
