from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .memory import MemoryStore
from .models import AgentState, MemoryRecord, SemanticStep, ValidationResult, WorkingMemory


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for mixed English and code text.

    Code tokenizes closer to one token per three characters; dividing by four
    systematically underestimated code-heavy prompts and could exceed a real model window.
    """
    return max(1, (len(text) + 2) // 3)


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
        working: WorkingMemory | None = None,
        step_tool_calls: int = 0,
        max_tool_calls: int = 0,
        evidence: Sequence[str] = (),
    ) -> Context:
        all_records = self.memory.all(state.run_id)
        # Pinned is lossless for what the user said and for accepted decisions; failures are
        # knowledge but unbounded, so only the most recent ones are pinned — the rest stay
        # retrievable in the store, never lost.
        failure_records = sorted(
            (record for record in all_records if record.kind == "failure"),
            key=lambda record: record.id or 0,
        )[-8:]
        pinned = [
            record
            for record in all_records
            if record.immutable or record.kind == "user_instruction"
        ] + failure_records
        # relevant memory is scoped to this repository: the store is cumulative across runs,
        # but another project's lessons must not enter this project's decision context
        relevant = (
            self.memory.search(state.objective, limit=20, source_repo=state.source_repo)
            if state.objective
            else []
        )
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
            f"Expected result: {step.expected_result if step else 'none'}",
            f"Expected evidence: {', '.join(step.expected_evidence) if step else 'none'}",
            f"Failure signals: {', '.join(step.failure_signals) if step else 'none'}",
            f"Latest user instruction: {state.latest_user_instruction or 'none'}",
        ]
        if max_tool_calls:
            header.append(f"Step tool calls: {step_tool_calls}/{max_tool_calls}")
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
        if working is not None:
            working_lines = []
            if working.active_files:
                working_lines.append(f"Working active files: {', '.join(working.active_files)}")
            if working.blocker:
                working_lines.append(f"Working blocker: {working.blocker}")
            if working.hypotheses:
                working_lines.append(f"Working hypotheses: {', '.join(working.hypotheses)}")
            if working.findings:
                working_lines.append(f"Working findings: {' | '.join(working.findings)}")
            if working.pending_validations:
                working_lines.append(
                    f"Working pending validations: {', '.join(working.pending_validations)}"
                )
            optional_lines.extend(working_lines)
        if current_diff:
            optional_lines.append(f"Current diff:\n{current_diff}")
        if validation is not None:
            optional_lines.extend(
                [
                    f"Validation passed: {validation.passed}",
                    f"Validation changed files: {', '.join(validation.changed_files)}",
                    f"Validation new files: {', '.join(validation.new_files)}",
                    f"Validation deleted files: {', '.join(validation.deleted_files)}",
                    f"Validation dependency changes: {', '.join(validation.dependency_changes)}",
                    f"Validation scope violations: {', '.join(validation.scope_violations)}",
                    f"Validation warnings: {', '.join(validation.warnings)}",
                    f"Validation diff stat: {validation.diff_stat}",
                    f"Validation details: {', '.join(validation.details)}",
                ]
            )
        optional_lines.extend(f"Observation: {item}" for item in observations[-5:])
        if evidence:
            optional_lines.append("Execution evidence (what actually happened):")
            optional_lines.extend(f"  {item}" for item in evidence[-8:])
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
