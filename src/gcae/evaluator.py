from __future__ import annotations

import json
from typing import Protocol

from .models import Evaluation, EvaluationInput
from .providers import Provider


class Evaluator(Protocol):
    def evaluate(self, payload: EvaluationInput) -> Evaluation: ...


class DeterministicEvaluator:
    """Default evaluator: deterministic rules over validation evidence."""

    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        validation = payload.validation
        if not validation.passed:
            return Evaluation(
                decision="rollback",
                reason="deterministic validation failed",
                progress_score=0.0,
            )
        return Evaluation(
            decision="accept",
            reason="deterministic validation passed",
            progress_score=1.0 if validation.changed_files else 0.0,
        )


def build_evaluation_prompt(payload: EvaluationInput) -> str:
    schema = json.dumps(Evaluation.model_json_schema(), separators=(",", ":"))
    validation = payload.validation
    evidence = {
        "passed": validation.passed,
        "diff_check_passed": validation.diff_check_passed,
        "changed_files": validation.changed_files,
        "new_files": validation.new_files,
        "deleted_files": validation.deleted_files,
        "dependency_changes": validation.dependency_changes,
        "scope_violations": validation.scope_violations,
        "details": validation.details,
        "command_results": [
            result.model_dump(mode="json") for result in validation.command_results
        ],
    }
    return (
        "You are the evaluator of GCAE, a reversible coding runtime. Judge the last semantic "
        "step and reply with exactly one JSON object and no other text.\n"
        "Decisions: accept (validated progress), rollback (incorrect, unnecessary, or validation "
        "failed), replan (blocked route or invalidated assumption), continue (more work needed in "
        "this step), finish_candidate (goal satisfied).\n"
        "When a step is rejected, put the failure lesson into memories_to_promote so it survives "
        "rollback.\n"
        "Assess correctness, requirement compliance, scope discipline, unnecessary architecture, "
        "repository hygiene, regressions, and invalidated assumptions.\n"
        f"Evaluation JSON schema: {schema}\n"
        f"Deterministic validation: {json.dumps(evidence, separators=(',', ':'))}\n"
        f"Context:\n{payload.context}"
    )


class LLMEvaluator:
    """Optional evaluator that asks the configured model for a validated Evaluation."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        return self.provider.complete(build_evaluation_prompt(payload), Evaluation)
