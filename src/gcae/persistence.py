from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AgentState


class StateStore:
    """Atomic JSON persistence for reversible execution state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def save(self, state: AgentState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = state.model_dump(mode="json")
        temp = self.path.with_suffix(self.path.suffix + ".write")
        temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def load(self) -> AgentState:
        return AgentState.model_validate_json(self.path.read_text(encoding="utf-8"))


class JsonlStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, sort_keys=True, default=str) + "\n")
