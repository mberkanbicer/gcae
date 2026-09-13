from pathlib import Path

from gcae.safeguards import RepetitionGuard, StagnationDetector, check_hygiene


def test_repeated_action_detection() -> None:
    guard = RepetitionGuard(limit=1)
    assert not guard.seen("read_file", {"path": "a"})
    assert guard.seen("read_file", {"path": "a"})


def test_stagnation_detection() -> None:
    detector = StagnationDetector(window=3)
    assert not detector.record(False)
    assert not detector.record(False)
    assert detector.record(False)


def test_hygiene_detection(tmp_path: Path) -> None:
    (tmp_path / "ok.txt").write_text("ok")
    assert check_hygiene(tmp_path).passed
    (tmp_path / "bad.pyc").write_bytes(b"x")
    assert not check_hygiene(tmp_path).passed
