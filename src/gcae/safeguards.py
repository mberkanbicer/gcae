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


class FailureRecurrence:
    """How often the same failure has come back, no matter which approach produced it.

    A per-approach fingerprint (tool plus arguments) is evaded by cosmetic changes to the
    command, and the tree changes on every edit — so the loop this guard exists to break
    (fix attempt, rerun, same error, another fix, same error) is only visible at the
    failure level, keyed by the normalized error signature alone.
    """

    def __init__(self, limit: int = 3) -> None:
        self.limit = limit
        self._counts: dict[str, int] = {}
        self._commands: dict[str, list[str]] = {}

    def record(self, signature: str, command: str = "") -> int:
        if not signature:
            return 0
        self._counts[signature] = self._counts.get(signature, 0) + 1
        commands = self._commands.setdefault(signature, [])
        if command and command not in commands:
            commands.append(command)
        return self._counts[signature]

    def exhausted(self, signature: str) -> bool:
        return bool(signature) and self._counts.get(signature, 0) >= self.limit

    def commands_for(self, signature: str) -> list[str]:
        """Every command that has produced this failure, first seen first."""
        return list(self._commands.get(signature, ()))

    def summary(self, limit: int = 5) -> list[tuple[str, int, list[str]]]:
        """Most-recurring failures first, each with the commands that produced it."""
        ranked = sorted(self._counts.items(), key=lambda item: (-item[1], item[0]))
        return [(sig, count, list(self._commands.get(sig, ()))) for sig, count in ranked[:limit]]


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
