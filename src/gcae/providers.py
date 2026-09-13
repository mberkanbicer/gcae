from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ValidationError

from .models import Decision

T = TypeVar("T", bound=BaseModel)


class Provider(Protocol):
    def complete(self, prompt: str, schema: type[T]) -> T: ...


class ProviderOutputError(RuntimeError):
    """Raised when bounded structured-output repair cannot recover a response."""


class FakeProvider:
    """Deterministic provider used by tests and offline demonstrations."""

    def __init__(self, outputs: Iterable[dict[str, Any] | str], repair_limit: int = 1) -> None:
        self._outputs = iter(outputs)
        self.repair_limit = repair_limit
        self.calls = 0
        self.repairs = 0

    def complete(self, prompt: str, schema: type[T]) -> T:
        del prompt
        self.calls += 1
        try:
            raw = next(self._outputs)
        except StopIteration as exc:
            raise ProviderOutputError("fake provider trajectory is exhausted") from exc
        try:
            return self._validate(raw, schema)
        except (ValidationError, TypeError, ValueError) as exc:
            if self.repairs >= self.repair_limit:
                raise ProviderOutputError(
                    "structured output remained invalid after bounded repair"
                ) from exc
            self.repairs += 1
            try:
                repaired = self._repair(raw)
            except (TypeError, ValueError) as repair_exc:
                raise ProviderOutputError("structured output repair failed") from repair_exc
            try:
                return self._validate(repaired, schema)
            except (ValidationError, TypeError, ValueError) as repair_exc:
                raise ProviderOutputError("structured output repair failed") from repair_exc

    @staticmethod
    def _validate(raw: dict[str, Any] | str, schema: type[T]) -> T:
        if isinstance(raw, str):
            raw = json.loads(raw)
        return schema.model_validate(raw)

    @staticmethod
    def _repair(raw: dict[str, Any] | str) -> dict[str, Any]:
        if isinstance(raw, str):
            value = cast(dict[str, Any], json.loads(raw))
        else:
            value = dict(raw)
        if value.get("action") == "execute" and "tool" in value:
            value["action"] = "execute_tool"
        value.setdefault("reason_summary", "repaired structured decision")
        value.setdefault("semantic_goal", "continue task")
        return value


class DecisionProvider:
    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def decide(self, context: str) -> Decision:
        return self.provider.complete(context, Decision)
