from __future__ import annotations

from .models import Decision, Evaluation, ValidationResult


class Evaluator:
    def evaluate(self, decision: Decision, validation: ValidationResult) -> Evaluation:
        if not validation.passed:
            return Evaluation(
                outcome="rollback",
                reason="deterministic validation failed",
                progress=False,
                requirement_compliant=False,
                clean=False,
            )
        if decision.action.value == "replan":
            return Evaluation(outcome="replan", reason=decision.reason_summary)
        if decision.action.value == "finish":
            return Evaluation(outcome="finish_candidate", reason=decision.reason_summary)
        return Evaluation(
            outcome="accept",
            reason="deterministic validation passed",
            progress=bool(validation.changed_files),
            requirement_compliant=True,
            clean=True,
        )
