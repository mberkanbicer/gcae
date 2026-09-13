from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .memory import MemoryStore
from .models import AgentState, MemoryRecord, SemanticStep, ValidationResult


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for mixed English and code text."""
    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class Context:
    text: str
    pinned_ids: tuple[int, ...]
    omitted_ids: tuple[int, ...] = ()


class ContextBuilder:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    def build(
        self,
        state: AgentState,
        step: SemanticStep | None = None,
        validation: ValidationResult | None = None,
        budget: int = 4096,
        current_diff: str = "",
        active_files: Sequence[str] = (),
        observations: Sequence[str] = (),
    ) -> Context:
        pinned = [
            record
            for record in self.memory.all(state.run_id)
            if record.immutable or record.kind in {"failure", "user_instruction"}
        ]
        relevant = self.memory.search(state.objective, limit=20) if state.objective else []
        records: list[MemoryRecord] = []
        seen: set[int] = set()
        for record in pinned + [candidate.record for candidate in relevant]:
            if record.id is not None and record.id not in seen:
                records.append(record)
                seen.add(record.id)

        header = [
            f"Objective: {state.objective}",
            f"Original request: {state.original_request}",
            f"Hard constraints: {', '.join(state.hard_constraints) or 'none'}",
            f"Success criteria: {', '.join(state.success_criteria) or 'none'}",
            f"Accepted commit: {state.accepted_commit or 'none'}",
            f"Current goal: {step.goal if step else 'none'}",
            f"Latest user instruction: {state.latest_user_instruction or 'none'}",
        ]
        pinned_ids = {
            record.id for record in pinned if record.id is not None
        }
        pinned_lines = header + [
            f"Memory[{record.id}|{record.kind}]: {record.content}"
            for record in records
            if record.id in pinned_ids
        ]
        optional_lines = [
            f"Plan: {plan.id} [{plan.status}] {plan.goal}"
            for plan in state.plan
        ]
        if active_files:
            optional_lines.append(f"Active files: {', '.join(active_files)}")
        if current_diff:
            optional_lines.append(f"Current diff:\n{current_diff}")
        if validation is not None:
            optional_lines.extend(
                [
                    f"Validation passed: {validation.passed}",
                    f"Validation changed files: {', '.join(validation.changed_files)}",
                    f"Validation details: {', '.join(validation.details)}",
                ]
            )
        optional_lines.extend(f"Observation: {item}" for item in observations[-5:])
        optional_lines.extend(
            f"Memory[{record.id}|{record.kind}]: {record.content}"
            for record in records
            if record.id not in pinned_ids
        )

        pinned_text = "\n".join(pinned_lines)
        if budget <= estimate_tokens(pinned_text):
            return Context(
                text=pinned_text,
                pinned_ids=tuple(sorted(pinned_ids)),
                omitted_ids=tuple(
                    record.id
                    for record in records
                    if record.id is not None and record.id not in pinned_ids
                ),
            )

        lines = [pinned_text]
        used = estimate_tokens(pinned_text)
        omitted: list[int] = []
        optional_records = [
            record for record in records if record.id is not None and record.id not in pinned_ids
        ]
        for line in optional_lines:
            separator_length = 1 if lines else 0
            cost = estimate_tokens(line) + separator_length
            if used + cost > budget:
                continue
            lines.append(line)
            used += cost
        rendered = "\n".join(lines)
        included_text = rendered
        for record in optional_records:
            rendered_line = f"Memory[{record.id}|{record.kind}]: {record.content}"
            if rendered_line not in included_text:
                omitted.append(record.id or 0)
        return Context(
            text=rendered,
            pinned_ids=tuple(sorted(pinned_ids)),
            omitted_ids=tuple(item for item in omitted if item > 0),
        )
