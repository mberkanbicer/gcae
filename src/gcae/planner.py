from __future__ import annotations

from .models import AgentState, PlanStep


class Planner:
    def plan(self, state: AgentState) -> list[PlanStep]:
        return [
            PlanStep(
                id="step-1",
                goal=state.objective,
                rationale="Implement the smallest change that satisfies the request.",
                expected_result="The requested behavior exists and is validated.",
                validation_requirements=state.success_criteria,
            )
        ]

    def replan(self, state: AgentState, reason: str) -> list[PlanStep]:
        numbers = [
            int(step.id.removeprefix("step-"))
            for step in state.plan
            if step.id.removeprefix("step-").isdigit()
        ]
        next_id = f"step-{max(numbers, default=0) + 1}"
        return [
            PlanStep(
                id=next_id,
                goal=state.objective,
                rationale=f"Replanned after: {reason}",
                expected_result="A corrected implementation passes deterministic validation.",
                validation_requirements=state.success_criteria,
            )
        ]
