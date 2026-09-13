from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def now_utc() -> datetime:
    return datetime.now(UTC)


class Action(StrEnum):
    EXECUTE_TOOL = "execute_tool"
    COMPLETE_SEMANTIC_STEP = "complete_semantic_step"
    REPLAN = "replan"
    FINISH_CANDIDATE = "finish_candidate"
    ASK_USER = "ask_user"


class RunPhase(StrEnum):
    ANALYZE = "analyze"
    PLAN = "plan"
    EXECUTE = "execute"
    VALIDATE = "validate"
    EVALUATE = "evaluate"
    CHECKPOINT = "checkpoint"
    ROLLBACK = "rollback"
    VERIFY = "verify"
    COMPLETE = "complete"
    FAILED = "failed"


class PlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    goal: str
    rationale: str = ""
    expected_result: str = ""
    intended_scope: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    status: Literal["pending", "active", "completed", "failed", "skipped"] = "pending"


class InitialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    success_criteria: list[str] = Field(default_factory=list)
    hard_constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)


class SemanticStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    goal: str
    rationale: str = ""
    expected_result: str = ""
    intended_scope: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    success: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    duration_ms: float | None = None
    artifact: str | None = None
    changed_files: list[str] = Field(default_factory=list)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: str
    summary: str
    artifact: str | None = None
    created_at: datetime = Field(default_factory=now_utc)


class WorkingMemory(BaseModel):
    model_config = ConfigDict(extra="forbid")
    hypotheses: list[str] = Field(default_factory=list)
    active_files: list[str] = Field(default_factory=list)
    blocker: str | None = None
    findings: list[str] = Field(default_factory=list)
    pending_validations: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    semantic_goal: str
    reason_summary: str
    tool: ToolCall | None = None
    expected_result: str = ""


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    command_results: list[ToolResult] = Field(default_factory=list)
    diff_check_passed: bool = True
    diff_stat: str = ""
    changed_files: list[str] = Field(default_factory=list)
    new_files: list[str] = Field(default_factory=list)
    deleted_files: list[str] = Field(default_factory=list)
    dependency_changes: list[str] = Field(default_factory=list)
    scope_violations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)


class MemoryRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int | None = None
    kind: str
    content: str
    run_id: str
    step_id: str | None = None
    source: str = "runtime"
    commit_sha: str | None = None
    created_at: datetime = Field(default_factory=now_utc)
    importance: int = Field(default=5, ge=0, le=10)
    immutable: bool = False


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record: MemoryRecord
    score: float = 0.0


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["accept", "rollback", "replan", "continue", "finish_candidate"]
    reason: str
    progress_score: float = 0.0
    next_goal: str | None = None
    memories_to_promote: list[MemoryCandidate] = Field(default_factory=list)


class EvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str
    semantic_goal: str
    hard_constraints: list[str] = Field(default_factory=list)
    latest_observations: list[str] = Field(default_factory=list)
    accepted_commit: str | None = None
    context: str = ""
    validation: ValidationResult


class CriterionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    criterion: str
    passed: bool
    evidence: str = ""


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    criteria: list[CriterionResult] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    hygiene_passed: bool = True
    details: list[str] = Field(default_factory=list)


class MergeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    branch: str
    target_branch: str
    pre_merge_commit: str
    merge_commit: str
    merged_at: datetime = Field(default_factory=now_utc)


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    timestamp: datetime = Field(default_factory=now_utc)
    run_id: str
    event_type: str
    phase: RunPhase | None = None
    step_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentState(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: str
    source_repo: str
    worktree: str
    branch: str
    objective: str
    original_request: str
    hard_constraints: list[str] = Field(default_factory=list)
    success_criteria: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    plan: list[PlanStep] = Field(default_factory=list)
    current_step_id: str | None = None
    accepted_commit: str | None = None
    phase: RunPhase = RunPhase.ANALYZE
    iteration: int = 0
    accepted_steps: int = 0
    next_step_number: int = 1
    status: str = "running"
    latest_user_instruction: str | None = None
    latest_observations: list[str] = Field(default_factory=list)
    working_memory: WorkingMemory = Field(default_factory=WorkingMemory)
    pending_question: str | None = None
    step_tool_calls: int = 0
    latest_validation: ValidationResult | None = None
    last_verification: VerificationReport | None = None
    merge: MergeRecord | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
