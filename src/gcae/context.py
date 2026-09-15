from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .memory import MemoryStore
from .models import (
    AgentState,
    MemoryCandidate,
    MemoryRecord,
    PlanStep,
    SemanticStep,
    ValidationResult,
    WorkingMemory,
)


def _normalize_memory(content: str) -> str:
    """Canonical form for the duplication gate: case, spacing and surrounding noise out."""
    return re.sub(r"\s+", " ", content.strip().lower())


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
    #: share of the rendered text that is retrieved memory (0..1)
    memory_share: float = 0.0
    #: relevant records dropped as duplicates during retrieval
    dropped_duplicates: int = 0
    #: relevant records retrieved before budgeting
    retrieved_total: int = 0


class ContextBuilder:
    def __init__(self, memory: MemoryStore) -> None:
        self.memory = memory

    @staticmethod
    def _plan_line(plan: PlanStep) -> str:
        """One compact plan row: locked history first, then the active trajectory."""
        marker = {
            "completed": "✓",
            "active": "●",
            "pending": "○",
            "invalidated": "×",
            "replaced": "↻",
        }.get(plan.status, "·")
        line = f"{marker} {plan.id} [{plan.status}] {plan.goal}"
        if plan.status == "invalidated" and plan.invalidated_reason:
            line += f" (invalidated: {plan.invalidated_reason[:80]})"
        return line

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
        # Retrieval tiers: this run first, then the same project, then explicitly
        # reusable global knowledge. Scoped FTS keeps other projects out entirely;
        # the unscoped pass only admits records marked global.
        relevant: list[MemoryCandidate] = []
        tier_of: dict[int, str] = {}

        def _take(candidates: list[MemoryCandidate], tier: str) -> None:
            for candidate in candidates:
                record = candidate.record
                if record.id is None or record.id in tier_of:
                    continue
                if tier == "global" and record.scope != "global":
                    continue
                tier_of[record.id] = tier
                relevant.append(candidate)

        if state.objective:
            scoped = self.memory.search(
                state.objective, limit=20, source_repo=state.source_repo
            )
            _take([c for c in scoped if c.record.run_id == state.run_id], "run")
            _take([c for c in scoped if c.record.run_id != state.run_id], "project")
            _take(self.memory.search(state.objective, limit=10), "global")
        tier_lines = {
            record.id: tier_of.get(record.id, "run")
            for record in pinned + [candidate.record for candidate in relevant]
            if record.id is not None
        }
        records: list[MemoryRecord] = []
        seen: set[int] = set()
        seen_content: set[str] = set()
        dropped_duplicates = 0
        for record in pinned + [candidate.record for candidate in relevant]:
            if record.id is None or record.id in seen:
                continue
            normalized = _normalize_memory(record.content)
            if normalized in seen_content:
                # relevance gate: the same lesson twice teaches nothing twice
                dropped_duplicates += 1
                continue
            records.append(record)
            seen.add(record.id)
            seen_content.add(normalized)

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
        def _memory_line(record: MemoryRecord) -> str:
            tier = tier_lines.get(record.id or 0, "run")
            return f"Memory[{record.id}|{record.kind}|{tier}]: {record.content}"

        pinned_lines = header + [
            _memory_line(record) for record in records if record.id in pinned_ids
        ]
        optional_lines = [
            f"Plan v{state.plan_version}:"
        ] + [
            f"  {self._plan_line(plan)} "
            for plan in state.plan
            if plan.status in {"pending", "active", "completed"}
            or (plan.status == "invalidated" and plan.invalidated_reason)
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
            _memory_line(record)
            for record in records
            if record.id not in pinned_ids
        )

        def _memory_share(text: str) -> float:
            memory_chars = sum(
                len(line) + 1 for line in text.splitlines() if line.startswith("Memory[")
            )
            return memory_chars / max(1, len(text))

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
                memory_share=_memory_share(pinned_text),
                dropped_duplicates=dropped_duplicates,
                retrieved_total=len(relevant),
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
            rendered_line = _memory_line(record)
            if rendered_line not in included_text:
                omitted.append(record.id or 0)
        return Context(
            text=rendered,
            pinned_ids=tuple(sorted(pinned_ids)),
            omitted_ids=tuple(item for item in omitted if item > 0),
            memory_share=_memory_share(rendered),
            dropped_duplicates=dropped_duplicates,
            retrieved_total=len(relevant),
        )
