from __future__ import annotations

import json
from typing import Protocol

from .models import AgentState, InitialPlan, PlanStep
from .providers import Provider


class PlannerLike(Protocol):
    def plan(self, state: AgentState) -> InitialPlan: ...

    def replan(self, state: AgentState, reason: str) -> list[PlanStep]: ...


def next_step(state: AgentState, reason: str) -> list[PlanStep]:
    """Create the next semantic step, preserving completed work and ids."""
    number = max(state.next_step_number, 1)
    state.next_step_number = number + 1
    return [
        PlanStep(
            id=f"step-{number}",
            goal=state.objective,
            rationale=f"Replanned after: {reason}",
            expected_result="A corrected implementation passes deterministic validation.",
            validation_requirements=state.success_criteria,
        )
    ]


class Planner:
    """Deterministic planner used when no model-backed planner is configured."""

    def plan(self, state: AgentState) -> InitialPlan:
        return InitialPlan(
            objective=state.objective,
            success_criteria=list(state.success_criteria),
            hard_constraints=list(state.hard_constraints),
            steps=[
                PlanStep(
                    id="step-1",
                    goal=state.objective,
                    rationale="Implement the smallest change that satisfies the request.",
                    expected_result="The requested behavior exists and is validated.",
                    validation_requirements=state.success_criteria,
                )
            ],
        )

    def replan(self, state: AgentState, reason: str) -> list[PlanStep]:
        return next_step(state, reason)


def build_planner_prompt(state: AgentState) -> str:
    schema = json.dumps(InitialPlan.model_json_schema(), separators=(",", ":"))
    return (
        "You are the planner of GCAE, a reversible coding runtime. Analyze the request and reply "
        "with exactly one JSON object and no other text.\n"
        "Determine the real objective, explicit and implicit success criteria, hard constraints, "
        "assumptions, and a short ordered plan of semantic steps.\n"
        "Success criteria must be checkable (prefer 'file exists: ', 'file contains: path :: text' "
        "or 'command succeeds: ' forms) and must include every user-provided criterion unchanged.\n"
        f"InitialPlan JSON schema: {schema}\n"
        f"User request: {state.original_request}\n"
        f"User-provided criteria: {json.dumps(state.success_criteria)}\n"
        f"User-provided constraints: {json.dumps(state.hard_constraints)}\n"
        f"Latest user instruction: {state.latest_user_instruction or 'none'}"
    )


class LLMPlanner:
    """Model-backed planner returning a validated InitialPlan."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider

    def plan(self, state: AgentState) -> InitialPlan:
        plan = self.provider.complete(build_planner_prompt(state), InitialPlan)
        criteria = list(state.success_criteria)
        for criterion in plan.success_criteria:
            if criterion not in criteria:
                criteria.append(criterion)
        constraints = list(state.hard_constraints)
        for constraint in plan.hard_constraints:
            if constraint not in constraints:
                constraints.append(constraint)
        steps = plan.steps or [PlanStep(id="step-1", goal=plan.objective or state.objective)]
        return plan.model_copy(
            update={
                "success_criteria": criteria,
                "hard_constraints": constraints,
                "steps": steps,
            }
        )

    def replan(self, state: AgentState, reason: str) -> list[PlanStep]:
        return next_step(state, reason)
