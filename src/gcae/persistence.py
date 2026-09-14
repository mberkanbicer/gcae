from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

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
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RuntimeError(f"run state is unreadable: {self.path} ({exc})") from exc
        try:
            return AgentState.model_validate_json(text)
        except (ValidationError, ValueError) as exc:
            # a half-written or edited state file: say exactly what is wrong and where the
            # remaining trace lives, instead of failing with a JSON stack trace
            events = self.path.with_name("events.jsonl")
            hint = (
                f" — the event log is intact: {events}"
                if events.exists()
                else " and no event log exists for this run"
            )
            raise RuntimeError(
                f"run state is corrupt or incomplete: {self.path} ({exc}){hint}"
            ) from exc
