from __future__ import annotations

from .models import AgentState, RunPhase

_ALLOWED: dict[RunPhase, set[RunPhase]] = {
    RunPhase.ANALYZE: {RunPhase.PLAN, RunPhase.FAILED},
    RunPhase.PLAN: {RunPhase.EXECUTE, RunPhase.FAILED},
    RunPhase.EXECUTE: {
        RunPhase.EXECUTE,
        RunPhase.PLAN,
        RunPhase.VALIDATE,
        RunPhase.VERIFY,
        RunPhase.FAILED,
    },
    RunPhase.VALIDATE: {RunPhase.EVALUATE, RunPhase.ROLLBACK, RunPhase.FAILED},
    RunPhase.EVALUATE: {
        RunPhase.EXECUTE,
        RunPhase.CHECKPOINT,
        RunPhase.ROLLBACK,
        RunPhase.PLAN,
        RunPhase.VERIFY,
        RunPhase.FAILED,
    },
    RunPhase.CHECKPOINT: {RunPhase.EXECUTE, RunPhase.VERIFY, RunPhase.FAILED},
    RunPhase.ROLLBACK: {RunPhase.EXECUTE, RunPhase.PLAN, RunPhase.FAILED},
    RunPhase.VERIFY: {RunPhase.COMPLETE, RunPhase.PLAN, RunPhase.FAILED},
    RunPhase.COMPLETE: set(),
    RunPhase.FAILED: set(),
}


class StateMachine:
    def transition(self, state: AgentState, target: RunPhase) -> AgentState:
        if target not in _ALLOWED[state.phase]:
            raise ValueError(f"invalid transition {state.phase} -> {target}")
        state.phase = target
        state.updated_at = state.updated_at.__class__.now(state.updated_at.tzinfo)
        return state

    def fail(self, state: AgentState, reason: str) -> AgentState:
        state.status = f"failed: {reason}"
        state.phase = RunPhase.FAILED
        return state
