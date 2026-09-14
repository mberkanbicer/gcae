from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path


class RepetitionGuard:
    def __init__(self, limit: int = 2) -> None:
        self.limit = limit
        self._counts: dict[str, int] = {}

    def seen(self, name: str, arguments: dict[str, object]) -> bool:
        key = json.dumps([name, arguments], sort_keys=True, default=str)
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key] > self.limit


class StagnationDetector:
    def __init__(self, window: int = 3) -> None:
        self.window = window
        self._progress: deque[bool] = deque(maxlen=window)

    def record(self, accepted_progress: bool) -> bool:
        self._progress.append(accepted_progress)
        return len(self._progress) == self.window and not any(self._progress)

    def reset(self) -> None:
        """Forget the current window: a corrected approach starts fresh."""
        self._progress.clear()


@dataclass(frozen=True)
class HygieneReport:
    passed: bool
    unexpected: tuple[str, ...]


def check_hygiene(worktree: str | Path) -> HygieneReport:
    root = Path(worktree)
    unexpected = tuple(
        str(path.relative_to(root))
        for path in root.rglob("*")
        if path.is_file()
        and (
            path.name.endswith("~")
            or path.name.endswith(".pyc")
            or path.name.endswith(".log")
        )
    )
    return HygieneReport(passed=not unexpected, unexpected=unexpected)
