from __future__ import annotations

import json
from typing import Protocol

from .models import AgentState, InitialPlan, PlanStep, ReplanPatch
from .providers import Provider, ProviderOutputError


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
            expected_evidence=["the corrected implementation passes its checks"],
            failure_signals=[f"the same failure repeats: {reason[:80]}"],
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
        "Success criteria must use exactly one of these forms and must never be empty: "
        "'file exists: path', 'file contains: path :: text', "
        "'file contains exactly: path :: text' (trailing newlines at end of file are ignored), "
        "'command succeeds: command'. "
        "Derive at least one criterion from the request itself and include every user-provided "
        "criterion unchanged.\n"
        "Each step's intended_scope must be repository-relative file or directory paths you "
        "expect that step to touch (for example 'fib.py' or 'src/parser.py'), and [] when "
        "you cannot say: scope is checked evidence, so prose there is useless.\n"
        "Each step should state expected_result (what succeeding looks like), up to three "
        "expected_evidence items (observable checks that would prove it, e.g. 'the test "
        "suite passes'), and up to three failure_signals (observations that would mean the "
        "step failed, e.g. 'the process exits immediately').\n"
        f"InitialPlan JSON schema: {schema}\n"
        f"User request: {state.original_request}\n"
        f"User-provided criteria: {json.dumps(state.success_criteria)}\n"
        f"User-provided constraints: {json.dumps(state.hard_constraints)}\n"
        f"Latest user instruction: {state.latest_user_instruction or 'none'}"
    )


def build_replan_prompt(
    state: AgentState,
    affected_from_step_id: str,
    reason: str,
    invalidated: list[dict[str, object]],
) -> str:
    """Prompt for a partial replan: the model patches the affected region only."""
    schema = json.dumps(ReplanPatch.model_json_schema(), separators=(",", ":"))
    locked = [
        {"id": step.id, "goal": step.goal}
        for step in state.plan
        if step.status == "completed"
    ]
    affected = [
        {"id": step.id, "goal": step.goal, "status": step.status}
        for step in state.plan
        if step.id == affected_from_step_id
        or step.status in {"pending", "active", "failed", "skipped"}
    ]
    return (
        "You are the replanner of GCAE, a reversible coding runtime. "
        "Reply with exactly one JSON object and no other text.\n"
        "Do not rewrite verified completed plan history. "
        "Preserve every completed verified step below unless the supplied evidence "
        "explicitly marks it as invalidated. "
        "Modify only the minimum affected current/future plan region. "
        "If an earlier completed step must be revisited, identify the exact step, "
        "the evidence that invalidates it, the rollback boundary, and the dependent "
        "steps. Do not restart the plan merely because a later step failed.\n"
        f"LOCKED VERIFIED HISTORY — DO NOT MODIFY: {json.dumps(locked)}\n"
        f"CURRENT AFFECTED REGION (replace from here): {json.dumps(affected)}\n"
        f"REPLAN REASON: {reason}\n"
        f"INVALIDATED ASSUMPTIONS WITH EVIDENCE: {json.dumps(invalidated)}\n"
        f"PLAN VERSION (echo it back as base_plan_version): {state.plan_version}\n"
        f"SUCCESS CRITERIA (every unresolved one must stay covered): "
        f"{json.dumps(state.success_criteria)}\n"
        f"ReplanPatch JSON schema: {schema}\n"
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
        if not criteria:
            raise ProviderOutputError("planner returned no success criteria")
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

    def replan_patch(
        self,
        state: AgentState,
        affected_from_step_id: str,
        reason: str,
        invalidated: list[dict[str, object]],
    ) -> ReplanPatch:
        """Ask the model for a structured patch of the affected region only."""
        prompt = build_replan_prompt(state, affected_from_step_id, reason, invalidated)
        return self.provider.complete(prompt, ReplanPatch)
