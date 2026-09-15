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
    #: what observable evidence should exist when the step succeeds
    expected_evidence: list[str] = Field(default_factory=list)
    #: observations that would contradict the expectation (checked, not decorative)
    failure_signals: list[str] = Field(default_factory=list)
    intended_scope: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)
    status: Literal[
        "pending", "active", "completed", "failed", "skipped",
        "invalidated", "replaced",
    ] = "pending"
    #: step IDs this step builds on; used to compute the minimum affected region
    depends_on: list[str] = Field(default_factory=list)
    #: accepted commit associated with this step at completion (or the trusted commit
    #: then current, for read-only steps that create no checkpoint)
    checkpoint: str | None = None
    #: a completed step with a checkpoint is locked: only explicit invalidation with
    #: evidence may reopen it, never a casual rewrite
    locked: bool = False
    #: id of the step that replaces this one, when status is "replaced"
    replaced_by: str = ""
    #: invalidation record: why, on what evidence (empty unless invalidated)
    invalidated_reason: str = ""
    invalidated_evidence_ids: list[int] = Field(default_factory=list)


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
    expected_evidence: list[str] = Field(default_factory=list)
    failure_signals: list[str] = Field(default_factory=list)
    intended_scope: list[str] = Field(default_factory=list)
    validation_requirements: list[str] = Field(default_factory=list)


class StepInvalidation(BaseModel):
    """One completed step reopened — only with recorded reason and evidence."""

    model_config = ConfigDict(extra="forbid")
    step_id: str
    reason: str = ""
    evidence_ids: list[int] = Field(default_factory=list)


class ReplanPatch(BaseModel):
    """A structured, bounded plan change: the affected segment only.

    The model proposes replacements for the affected region; the runtime validates
    and applies them deterministically. Unchanged steps are never resent."""

    model_config = ConfigDict(extra="forbid")
    base_plan_version: int = 1
    reason: str = ""
    reason_category: Literal[
        "invalid_assumption", "failure", "new_evidence", "user_override",
        "dependency_invalidated", "blocked_path", "requirement_change",
        "verified_better_route",
    ] = "failure"
    affected_from_step_id: str = ""
    preserve_step_ids: list[str] = Field(default_factory=list)
    invalidate: list[StepInvalidation] = Field(default_factory=list)
    replace_step_ids: list[str] = Field(default_factory=list)
    new_steps: list[PlanStep] = Field(default_factory=list)
    skip_step_ids: list[str] = Field(default_factory=list)
    rollback_required: bool = False
    rollback_target_checkpoint: str | None = None
    success_criteria_coverage: list[str] = Field(default_factory=list)


class PlanVersion(BaseModel):
    """One persisted plan revision: what stayed, what changed, and why."""

    model_config = ConfigDict(extra="forbid")
    version: int
    reason: str = ""
    reason_category: str = "failure"
    preserved: list[str] = Field(default_factory=list)
    invalidated: list[str] = Field(default_factory=list)
    replaced: list[str] = Field(default_factory=list)
    inserted: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


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
    # execution evidence: how the command ran and what it told us
    mode: str = ""
    cwd: str = ""
    timed_out: bool = False
    timeout_kind: str = ""
    interactive_detected: bool = False
    waiting_for_input: bool = False
    prompt: str = ""
    termination_reason: str = ""
    stdin_sent: int = 0


class EvidenceKind(StrEnum):
    """Evidence classes, kept small: a kind exists only when something displays it differently."""

    COMMAND_RESULT = "command_result"
    TEST_RESULT = "test_result"
    BUILD_RESULT = "build_result"
    FILE_STATE = "file_state"
    GIT_DIFF = "git_diff"
    STATIC_CHECK = "static_check"
    INTERACTIVE_SESSION = "interactive_session"
    ARTIFACT = "artifact"
    USER_CONFIRMATION = "user_confirmation"
    OBSERVATION = "observation"


class EvidenceRecord(BaseModel):
    """One piece of observable evidence in the run's evidence ledger.

    Deliberately not a knowledge graph and not scored: evidence is present, absent,
    supporting, or contradicting.  ``supports`` and ``contradicts`` hold the claims
    (expectations or success criteria) the evidence speaks for or against.
    """

    model_config = ConfigDict(extra="forbid")
    id: int | None = None
    run_id: str
    trajectory_step_id: str = ""
    kind: EvidenceKind
    claim_or_subject: str = ""
    source_type: str = ""
    source_reference: str = ""
    summary: str = ""
    supports: list[str] = Field(default_factory=list)
    contradicts: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=now_utc)


class FailureSignal(BaseModel):
    """One classified failure, with the lesson the next attempt must respect."""

    model_config = ConfigDict(extra="forbid")
    kind: str
    lesson: str
    signature: str = ""
    command: str = ""
    evidence: str = ""
    created_at: datetime = Field(default_factory=now_utc)


class PendingInput(BaseModel):
    """A live process waiting for an answer only the user can give."""

    model_config = ConfigDict(extra="forbid")
    command: str
    prompt: str
    goal: str = ""
    mode: str = ""
    sensitive: bool = False
    created_at: datetime = Field(default_factory=now_utc)


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


class TrajectoryStepStatus(StrEnum):
    """The life of one semantic attempt: from intention to a trusted-state transition."""

    PREPARING = "preparing"
    EXECUTING = "executing"
    OBSERVING = "observing"
    VALIDATING = "validating"
    EVALUATING = "evaluating"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REPAIRED = "repaired"
    REPLANNED = "replanned"
    BLOCKED = "blocked"


class TrajectoryStep(BaseModel):
    """The primary execution entity: what one semantic attempt tried, did, observed and learned.

    A trajectory step answers, from persisted state alone: what was the goal, what was
    expected, what happened, what evidence was collected, why was the candidate accepted or
    rejected, and what did the system learn — without consulting any chat history.
    """

    model_config = ConfigDict(extra="forbid")
    id: str
    semantic_goal: str
    parent_plan_step_id: str
    started_at: datetime = Field(default_factory=now_utc)
    completed_at: datetime | None = None
    expectation: str = ""
    expected_evidence: list[str] = Field(default_factory=list)
    failure_signals: list[str] = Field(default_factory=list)
    #: bounded summaries of the actions taken (tool, one line each)
    actions: list[str] = Field(default_factory=list)
    #: bounded summaries of what was observed
    observations: list[str] = Field(default_factory=list)
    #: outcome of deterministic validation (pass/fail plus one-line detail)
    validations: list[str] = Field(default_factory=list)
    evidence_ids: list[int] = Field(default_factory=list)
    candidate_base_commit: str = ""
    candidate_result_commit: str | None = None
    decision: str = ""
    decision_reason: str = ""
    knowledge_gained: list[str] = Field(default_factory=list)
    status: TrajectoryStepStatus = TrajectoryStepStatus.PREPARING


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Action
    semantic_goal: str
    reason_summary: str
    tool: ToolCall | None = None
    expected_result: str = ""
    #: the controller believes the trajectory itself must change. The flag only
    #: *requests* a replan; the dedicated replanning mechanism mutates the plan.
    replan_required: bool = False


class ValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    commands: list[str] = Field(default_factory=list)
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
    #: the repository the knowledge came from; retrieval is scoped to it, so lessons from
    #: one project never leak into another project's decision context
    source_repo: str = ""
    #: visibility tier: "run" (this run only), "project" (same repository), "global"
    #: (explicitly reusable across projects — nothing is global unless marked so)
    scope: Literal["run", "project", "global"] = "run"
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
    decision: Literal["accept", "repair", "rollback", "replan", "continue", "finish_candidate"]
    reason: str
    progress_score: float = 0.0
    next_goal: str | None = None
    memories_to_promote: list[MemoryCandidate] = Field(default_factory=list)
    #: replan request detail: which step is affected, which assumption died, on what evidence.
    #: The evaluator requests; only the replanning mechanism rewrites the plan.
    affected_step_id: str = ""
    invalidated_assumption: str = ""
    evidence_ids: list[int] = Field(default_factory=list)


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
    #: PASS / FAIL / INSUFFICIENT_EVIDENCE — completion requires PASS for every criterion
    status: Literal["pass", "fail", "insufficient"] = "fail"
    evidence: str = ""
    #: ledger records that support this verdict
    evidence_ids: list[int] = Field(default_factory=list)


class CriterionJudgement(BaseModel):
    """Structured verdict from the model judge for a non-deterministic criterion."""

    model_config = ConfigDict(extra="forbid")
    passed: bool
    evidence: str = ""


class VerificationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    passed: bool
    criteria: list[CriterionResult] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    hygiene_passed: bool = True
    details: list[str] = Field(default_factory=list)


class Diagnosis(BaseModel):
    """The recovery advisor's structured reading of a failing run's own trace."""

    model_config = ConfigDict(extra="forbid")
    root_cause: str
    corrective_instruction: str
    strategy: Literal["replan", "ask_user", "stop"] = "replan"
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    # optional: when the planner could not produce criteria, the advisor may propose them
    success_criteria: list[str] = Field(default_factory=list)


class RecoveryRecord(BaseModel):
    """One self-diagnosis, persisted so a run can explain its own recovery."""

    model_config = ConfigDict(extra="forbid")
    attempt: int
    trigger: str
    root_cause: str
    corrective_instruction: str
    strategy: str
    model: str = ""
    created_at: datetime = Field(default_factory=now_utc)


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
    #: active plan revision; every replan bumps it and records a PlanVersion
    plan_version: int = 1
    #: bounded audit trail of plan revisions (newest last)
    plan_history: list[PlanVersion] = Field(default_factory=list)
    #: success criteria verified against evidence and unaffected since
    verified_criteria: list[str] = Field(default_factory=list)
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
    #: the persisted trajectory: the run's semantic attempts, newest last (bounded)
    trajectory: list[TrajectoryStep] = Field(default_factory=list)
    step_tool_calls: int = 0
    #: commands actually executed during the current step (evidence, not intent)
    step_commands: int = 0
    latest_validation: ValidationResult | None = None
    last_verification: VerificationReport | None = None
    recovery: RecoveryRecord | None = None
    #: the last classified execution failure, so the next decision can act on it
    last_failure: FailureSignal | None = None
    #: set while a process is alive and waiting for input only the user can provide
    pending_input: PendingInput | None = None
    #: why the run cannot continue autonomously, and what would unblock it
    blocked_reason: str | None = None
    unblock_hint: str | None = None
    # non-essential subsystems that failed during the run (state file, memory, event log):
    # recorded so the result is never silently incomplete
    degradations: list[str] = Field(default_factory=list)
    merge: MergeRecord | None = None
    created_at: datetime = Field(default_factory=now_utc)
    updated_at: datetime = Field(default_factory=now_utc)
