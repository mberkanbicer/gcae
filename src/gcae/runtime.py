from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path
from typing import IO, Any

from .context import ContextBuilder, estimate_tokens
from .controller import Controller
from .evaluator import DeterministicEvaluator, Evaluator
from .git import GitError, GitRepository, NothingToMerge
from .memory import EventLog, MemoryStore
from .models import (
    Action,
    AgentState,
    Decision,
    Evaluation,
    EvaluationInput,
    Event,
    MemoryCandidate,
    MemoryRecord,
    MergeRecord,
    Observation,
    PlanStep,
    RecoveryRecord,
    RunPhase,
    SemanticStep,
    ToolResult,
    ValidationResult,
    WorkingMemory,
    now_utc,
)
from .persistence import StateStore
from .planner import Planner, PlannerLike, next_step
from .providers import (
    DecisionProvider,
    FakeProvider,
    Provider,
    ProviderOutputError,
    StreamProgress,
)
from .recovery import RecoveryAction, RecoveryAdvisor, build_trace
from .safeguards import RepetitionGuard, StagnationDetector
from .state_machine import StateMachine
from .tools import ToolRegistry
from .validation import DeterministicValidator
from .verifier import FinalVerifier

logger = logging.getLogger("gcae")

# phases with a one-hop legal transition to PLAN (see state_machine._ALLOWED)
_DIRECT_TO_PLAN = frozenset(
    {
        RunPhase.ANALYZE,
        RunPhase.PLAN,
        RunPhase.EXECUTE,
        RunPhase.EVALUATE,
        RunPhase.ROLLBACK,
        RunPhase.VERIFY,
        RunPhase.COMPLETE,
    }
)

# How often a provider call reports itself while it has produced nothing. Keeps the event log
# and the dashboard moving during a long deliberation instead of showing a frozen screen.
PROVIDER_HEARTBEAT_SECONDS = 10.0

EventSubscriber = Callable[[Event], None]


def cleanup_idle_worktree(repo: GitRepository, state: AgentState) -> bool:
    """Remove GCAE's own worktree once the run no longer needs it. True when removed.

    Used when the run's work is delivered (merged) or empty (nothing to merge). The branch
    is deliberately kept when a merge happened: the merge stays reversible with
    ``gcae undo`` and the accepted commits can be merged again.
    """
    path = Path(state.worktree)
    if not path.exists():
        return False
    repo.worktree = path
    repo.remove_worktree()
    return True


def cleanup_merged_worktree(repo: GitRepository, state: AgentState) -> bool:
    """Remove the worktree of a merged run (see :func:`cleanup_idle_worktree`)."""
    if state.merge is None:
        return False
    return cleanup_idle_worktree(repo, state)


def merge_verified_run(
    repo: GitRepository,
    state: AgentState,
    persist: Callable[[], None],
    allow_unverified: bool = False,
) -> MergeRecord:
    """Guarded merge of a run branch into the current source branch.

    Shared by the runtime (after completion) and ``gcae merge`` so both paths apply the
    same checks: the run must be complete, unmerged, and its branch must still point at
    the verified commit; the source repository must be clean.

    ``allow_unverified`` additionally permits a failed or stopped run that accepted at
    least one checkpoint: the user asked for the accepted work back explicitly, and the
    caller reports that final verification did not pass.
    """
    unverified = state.status != "complete"
    if unverified and not (allow_unverified and state.accepted_steps > 0):
        raise RuntimeError(f"run {state.run_id} is not complete: {state.status}")
    if unverified and state.merge is None:
        logger.warning(
            "merging accepted work from run %s without final verification (%s)",
            state.run_id,
            state.status,
        )
    if state.merge is not None:
        raise RuntimeError(
            f"run {state.run_id} is already merged into {state.merge.target_branch}; "
            f"run 'gcae undo {state.source_repo} {state.run_id}' first"
        )
    if not state.branch:
        raise RuntimeError(f"run {state.run_id} has no branch to merge")
    if state.accepted_commit:
        head = repo.branch_head(state.branch)
        if head != state.accepted_commit:
            raise RuntimeError(
                f"branch {state.branch} moved past the verified commit "
                f"{state.accepted_commit[:12]}; refusing to merge"
            )
    if state.branch and repo.branch_head(state.branch) == repo.source_commit():
        raise NothingToMerge(
            f"run {state.run_id} produced no file changes; there is nothing to merge"
        )
    if repo.source_status_entries():
        # GCAE never leaves the user to stash their own work: commit it as the base the
        # merge builds on (bounded, reported, reversible with git reset --soft HEAD~1).
        repo.bootstrap_source_repository()
    target = repo.current_branch()
    pre, merged = repo.merge_branch(state.branch)
    record = MergeRecord(
        branch=state.branch,
        target_branch=target,
        pre_merge_commit=pre,
        merge_commit=merged,
    )
    state.merge = record
    persist()
    return record


class UnresumableStateError(RuntimeError):
    """The run's own state could not be written, so the run must stop.

    Everything else the runtime owns degrades; state does not.  A run whose ``state.json`` is
    stale still *looks* resumable, and ``gcae resume`` would continue from an older checkpoint
    than the branch actually holds.  Stopping with the reason recorded is the honest answer:
    accepted commits stay on the branch, and the event log explains what happened.
    """


class RunLock:
    """One GCAE run per source repository, across processes.

    Two runs sharing a repository would interleave their merges into the source branch.  The
    lock file lives in the runtime directory (never in the repository), is held for the run,
    and is released by the operating system if the process dies.  Re-entrant inside the
    process so a resumed or re-started run does not fight its own predecessor.
    """

    _held: dict[str, int] = {}

    def __init__(self, runtime_dir: Path, source_repo: Path, label: str = "gcae") -> None:
        self.source_repo = source_repo
        self.label = label
        digest = hashlib.sha256(str(Path(source_repo).resolve()).encode()).hexdigest()[:16]
        self.path = Path(runtime_dir) / "locks" / f"{digest}.lock"
        self._handle: IO[str] | None = None
        self._reentrant = False
        self._key = str(self.path)

    def acquire(self) -> None:
        if RunLock._held.get(self._key):
            RunLock._held[self._key] += 1
            self._reentrant = True
            return
        try:
            import fcntl
        except ImportError:  # pragma: no cover - the runtime targets POSIX
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.seek(0)
            held_by = handle.read().strip()
            handle.close()
            detail = f" ({held_by})" if held_by else ""
            raise RuntimeError(
                f"another GCAE run is already working on {self.source_repo}{detail} — wait for "
                "it to finish before starting a second run on the same repository"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"{self.label}\n")
        handle.flush()
        self._handle = handle
        RunLock._held[self._key] = 1

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def release(self) -> None:
        if self._reentrant:
            RunLock._held[self._key] = max(0, RunLock._held.get(self._key, 1) - 1)
            self._reentrant = False
            return
        if self._handle is None:
            return
        RunLock._held.pop(self._key, None)
        try:
            self._handle.close()  # closing the descriptor releases the flock
        except OSError:
            logger.debug("could not close the run lock", exc_info=True)
        self._handle = None


class RuntimeControl:
    """Thread-safe pause / stop / user-instruction channel shared with a UI."""

    def __init__(self) -> None:
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._instructions: list[str] = []
        self._lock = threading.Lock()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def stop(self) -> None:
        self._stop.set()

    def submit_instruction(self, text: str) -> None:
        with self._lock:
            self._instructions.append(text)

    def take_instructions(self) -> list[str]:
        with self._lock:
            pending, self._instructions = self._instructions, []
        return pending

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()


class Runtime:
    def __init__(
        self,
        source_repo: str | Path,
        runtime_dir: str | Path,
        worktree_dir: str | Path | None = None,
        provider: Provider | None = None,
        validator_commands: list[str] | None = None,
        max_steps: int = 20,
        command_timeout: int = 30,
        context_limit: int = 8192,
        evaluator: Evaluator | None = None,
        planner: PlannerLike | None = None,
        verifier: FinalVerifier | None = None,
        max_tool_calls_per_step: int = 8,
        stagnation_window: int = 3,
        repetition_limit: int = 2,
        scope_warning_files: int = 10,
        control: RuntimeControl | None = None,
        role_providers: dict[str, Provider] | None = None,
        provider_label: str = "",
        auto_bootstrap: bool = True,
        auto_merge: bool = True,
        merge_accepted_on_failure: bool = True,
        cleanup_after_merge: bool = True,
        resolve_merge_conflicts: bool = True,
        recovery_attempts: int = 2,
        recovery_budget: int = 5,
    ) -> None:
        self.source_repo = Path(source_repo).resolve()
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.worktree_dir = (
            Path(worktree_dir).expanduser().resolve() if worktree_dir is not None else None
        )
        self.provider = provider or FakeProvider(
            [
                {
                    "action": "finish_candidate",
                    "semantic_goal": "finish",
                    "reason_summary": "offline",
                }
            ]
        )
        self.role_providers = dict(role_providers or {})
        self.provider_label = provider_label
        self.auto_bootstrap = auto_bootstrap
        self.auto_merge = auto_merge
        self.merge_accepted_on_failure = merge_accepted_on_failure
        self.cleanup_after_merge = cleanup_after_merge
        self.auto_resolve_conflicts = resolve_merge_conflicts
        self._conflict_resolution = False
        self._conflict_failed = False
        self._stagnation_escalated = False
        self._stagnation_asked = False
        # self-recovery: how often this session may diagnose itself, and how many extra
        # iterations each successful diagnosis buys.
        self.recovery_attempts = recovery_attempts
        self.recovery_budget = recovery_budget
        self._recoveries = 0
        self._recovery_extensions = 0
        self._decision_error: str | None = None
        # repeated identical rejections are stagnation even when unrelated steps are accepted
        # in between (observed: 20 iterations rewriting the same file after the same rollback)
        self._last_failure_signature = ""
        self._same_failure_count = 0
        self.repeated_failure_limit = 3
        self._degrade_notified: dict[str, bool] = {}
        self._failed_over: set[str] = set()
        self._run_lock: RunLock | None = None
        self.validator_commands = list(validator_commands or [])
        self.max_steps = max_steps
        self.command_timeout = command_timeout
        self.context_limit = context_limit
        self.evaluator = evaluator or DeterministicEvaluator()
        self.planner = planner or Planner()
        self.max_tool_calls_per_step = max_tool_calls_per_step
        self.repetition_limit = repetition_limit
        self.scope_warning_files = scope_warning_files
        self.control = control
        self.machine = StateMachine()
        self.verifier = verifier or FinalVerifier()
        self.state: AgentState | None = None
        self.repo: GitRepository | None = None
        self.memory: MemoryStore | None = None
        self.events: EventLog | None = None
        self.repetition = RepetitionGuard(repetition_limit)
        self.stagnation = StagnationDetector(stagnation_window)
        self._subscribers: list[EventSubscriber] = []
        self._trace: deque[Event] = deque(maxlen=200)
        self._consecutive_failures = 0
        self._escalated = False
        self.last_context_info: dict[str, int] = {}
        self.last_context_text = ""
        self._planner_notice: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def subscribe(self, callback: EventSubscriber) -> None:
        self._subscribers.append(callback)

    def provider_for(self, role: str) -> Provider:
        if role == "controller" and self._escalated:
            return self.role_providers.get("escalation", self.provider)
        return self.role_providers.get(role, self.provider)

    def active_model(self, role: str = "controller") -> str:
        provider = self.provider_for(role)
        return str(getattr(provider, "model", provider.__class__.__name__))

    def role_model(self, role: str) -> str | None:
        """Model actually used for a role; None when the role needs no model."""
        if role == "controller":
            return self.active_model("controller")
        if role == "planner":
            provider = getattr(self.planner, "provider", None)
        elif role == "evaluator":
            provider = getattr(self.evaluator, "provider", None)
        elif role == "verifier":
            provider = getattr(self.verifier, "judge", None)
        else:
            provider = self.role_providers.get(role)
        if provider is None:
            return None
        return str(getattr(provider, "model", provider.__class__.__name__))

    def start(
        self,
        request: str,
        hard_constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        run_id: str | None = None,
    ) -> AgentState:
        run_id = run_id or uuid.uuid4().hex[:12]
        self.release_lock()
        self._run_lock = RunLock(self.runtime_dir, self.source_repo, f"run {run_id}")
        self._run_lock.acquire()
        self.repo = GitRepository(
            self.source_repo,
            self.runtime_dir,
            self.worktree_dir,
            auto_bootstrap=self.auto_bootstrap,
        )
        worktree, branch, base = self.repo.create_isolated_worktree(run_id)
        try:
            self.memory = MemoryStore(self.runtime_dir / "memory.db")
            self.events = EventLog(self.runtime_dir / "runs" / run_id / "events.jsonl")
        except OSError as exc:
            raise RuntimeError(
                f"runtime state directory is not writable: {self.runtime_dir} ({exc})"
            ) from exc
        logger.info("run %s started on branch %s in %s", run_id, branch, worktree)
        self.state = AgentState(
            run_id=run_id,
            source_repo=str(self.source_repo),
            worktree=str(worktree),
            branch=branch,
            objective=request,
            original_request=request,
            hard_constraints=list(hard_constraints or []),
            success_criteria=list(success_criteria or []),
            accepted_commit=base,
            latest_user_instruction=request,
        )
        # persist before the planner runs: the run must be discoverable (`gcae list`) and
        # resumable even if the model call is slow, stalls, or the process dies while it waits
        self._persist()
        try:
            with self._progress("planner", getattr(self.planner, "provider", None)):
                initial_plan = self.planner.plan(self.state)
        except ProviderOutputError as exc:
            self._remember("failure", f"planner failure: {exc}", immutable=True)
            if self.state.success_criteria:
                # user criteria define done; a planner outage must not waste the whole run
                initial_plan = Planner().plan(self.state)
                self._planner_notice = str(exc)
                logger.warning("planner unavailable, using the deterministic plan: %s", exc)
            else:
                # no criteria and no plan: the recovery advisor may supply both, which keeps a
                # planner outage recoverable instead of a dead end
                self._event(
                    "recovery_started",
                    RunPhase.ANALYZE,
                    payload={"trigger": f"planner output: {exc}", "attempt": 1},
                )
                outcome = self._recover(f"planner output: {exc}", None)
                if outcome is RecoveryAction.CONTINUE and self.state.success_criteria:
                    initial_plan = Planner().plan(self.state)
                    self._planner_notice = (
                        f"planner failed ({exc}); the recovery advisor supplied the criteria"
                    )
                    logger.warning("planner failed; recovery supplied criteria and a step")
                elif outcome is RecoveryAction.ASK_USER:
                    raise RuntimeError(
                        f"planner failed and the run needs your input: "
                        f"{self.state.pending_question}"
                    ) from exc
                else:
                    self.state.status = "failed: planner output"
                    self.state.phase = RunPhase.FAILED
                    self._persist()
                    raise RuntimeError(
                        f"planner failed: {exc} — re-run with a checkable --criterion "
                        '(e.g. --criterion "file exists: README.md") to start without the planner'
                    ) from exc
        self.state.objective = initial_plan.objective or request
        self.state.assumptions = list(initial_plan.assumptions)
        for criterion in initial_plan.success_criteria:
            if criterion not in self.state.success_criteria:
                self.state.success_criteria.append(criterion)
        for constraint in initial_plan.hard_constraints:
            if constraint not in self.state.hard_constraints:
                self.state.hard_constraints.append(constraint)
        self.state.plan = list(initial_plan.steps)
        numbers = [
            int(step.id.removeprefix("step-"))
            for step in self.state.plan
            if step.id.removeprefix("step-").isdigit()
        ]
        self.state.next_step_number = max(numbers, default=0) + 1
        self._transition(RunPhase.PLAN)
        self._publish_repository_notices()
        if self._planner_notice is not None:
            self._event(
                "planner_fallback",
                RunPhase.PLAN,
                payload={"reason": self._planner_notice},
            )
            self._planner_notice = None
        self._emit_plan(reason="initial plan")
        self._remember("user_instruction", request, immutable=True)
        for constraint in self.state.hard_constraints:
            self._remember("user_instruction", f"hard constraint: {constraint}", immutable=True)
        for criterion in self.state.success_criteria:
            self._remember("user_instruction", f"success criterion: {criterion}", immutable=True)
        self._event("run_started", RunPhase.ANALYZE, payload={"objective": self.state.objective})
        self._emit_candidate_state()
        self._persist()
        return self.state

    def resume(self, run_id: str) -> AgentState:
        path = self.runtime_dir / "runs" / run_id
        self.release_lock()
        self._run_lock = RunLock(self.runtime_dir, self.source_repo, f"run {run_id}")
        self._run_lock.acquire()
        self.state = StateStore(path / "state.json").load()
        if Path(self.state.source_repo).resolve() != self.source_repo:
            raise RuntimeError("resume source repository does not match persisted state")
        self.repo = GitRepository(
            self.source_repo,
            self.runtime_dir,
            self.worktree_dir,
            auto_bootstrap=self.auto_bootstrap,
        )
        self.repo.worktree = Path(self.state.worktree)
        self.repo.branch = self.state.branch
        self.memory = MemoryStore(self.runtime_dir / "memory.db")
        self.events = EventLog(path / "events.jsonl")
        self.repo.validate_source()
        # GCAE owns its worktree: recreate it from the run branch instead of giving up
        self.repo.ensure_worktree(self.state.branch, Path(self.state.worktree))
        if self.state.accepted_commit:
            self.repo.rollback(self.state.accepted_commit)
        if self.state.status != "complete":
            self.state.status = "running"
            self.state.phase = RunPhase.PLAN
            self.state.pending_question = None
            self.state.step_tool_calls = 0
            self._persist()
        self._publish_repository_notices()
        self._event("run_resumed", self.state.phase, payload={"run_id": run_id})
        return self.state

    def inject_user_instruction(self, text: str) -> AgentState:
        """Record a user override, discard speculative work and replan."""
        if self.state is None:
            raise RuntimeError("call start or resume before injecting an instruction")
        self.state.latest_user_instruction = text
        self.state.pending_question = None
        if not self.state.status.startswith("complete"):
            # an override re-arms a run that stalled waiting for the user
            self.state.status = "running"
        self._remember("user_instruction", f"user override: {text}", immutable=True)
        if self.repo is not None and self.repo.worktree is not None and self.repo.status():
            self._rollback(f"user override: {text}")
        for plan in self.state.plan:
            if plan.status in {"pending", "active"}:
                plan.status = "skipped"
        self.state.plan = [plan for plan in self.state.plan if plan.status != "skipped"]
        self.state.plan.extend(next_step(self.state, f"user override: {text}"))
        self._reset_working_memory()
        self._transition(RunPhase.PLAN)
        self._emit_plan(reason=f"user override: {text}")
        self._event("user_override", RunPhase.PLAN, payload={"text": text})
        logger.info("user override: %s", text)
        self._persist()
        return self.state

    # ------------------------------------------------------------------ main loop

    def run(self) -> AgentState:
        """Drive the run to a terminal state.

        An unexpected exception is a *run* failure, not a process death: it is recorded,
        handed to the recovery advisor like every other fatal condition, and only then
        allowed to end the run. Accepted checkpoints are never touched.
        """
        if self.state is None:
            raise RuntimeError("call start or resume before run")
        if self.state.status == "complete":
            return self.state
        assert self.repo is not None and self.memory is not None

        while True:
            try:
                return self._run_loop()
            except UnresumableStateError as exc:
                # the one failure recovery must not swallow: diagnosing it would need the same
                # write that just failed, and continuing would leave resume pointing backwards
                self._remember("failure", f"unresumable state: {exc}", immutable=True)
                return self._fail(str(exc), persist=False)
            except Exception as exc:  # noqa: BLE001 - crashes are handled, not swallowed
                logger.exception("unexpected error in run %s", self.state.run_id)
                reason = f"{type(exc).__name__}: {exc}"
                self._remember("failure", f"unexpected error: {reason}", immutable=True)
                outcome = self._recover(
                    f"unexpected error: {reason}", self._current_plan_step()
                )
                if outcome is RecoveryAction.CONTINUE:
                    continue
                if outcome is RecoveryAction.UNAVAILABLE:
                    return self._fail(f"unexpected {reason}")
                return self.state

    def _run_loop(self) -> AgentState:
        assert self.state is not None and self.repo is not None and self.memory is not None
        iterations = 0
        while True:
            if self._pump_control():
                return self.state
            self.state.iteration += 1
            plan = self._current_plan_step()
            if plan is None:
                # A finished plan is verified even when the iteration budget is exhausted:
                # the work is done, so diagnosing an "exhausted budget" here burned a
                # self-diagnosis and a redundant re-planned step before verifying anyway.
                if self._verify_and_route("final verification failed"):
                    return self.state
                continue
            if iterations >= self._step_budget():
                budget_message = (
                    f"step budget exhausted after {iterations} iterations "
                    "(raise [runtime] max_steps for longer tasks)"
                )
                outcome = self._recover(budget_message, None)
                if outcome is RecoveryAction.CONTINUE:
                    continue
                if outcome is RecoveryAction.UNAVAILABLE:
                    if self.repo.status():
                        self._rollback(budget_message)
                    return self._fail(budget_message)
                return self.state
            iterations += 1

            self.state.current_step_id = plan.id
            if plan.status == "pending":
                plan.status = "active"
                self.state.step_tool_calls = 0
                self.repetition = RepetitionGuard(self.repetition_limit)
                self.state.working_memory.pending_validations = list(
                    plan.validation_requirements
                )
                position = next(
                    (
                        index
                        for index, item in enumerate(self.state.plan)
                        if item.id == plan.id
                    ),
                    0,
                )
                self._event(
                    "step_started",
                    RunPhase.EXECUTE,
                    step_id=plan.id,
                    payload={
                        "goal": plan.goal,
                        "index": position + 1,
                        "total": len(self.state.plan),
                    },
                )
            self._transition(RunPhase.EXECUTE)
            step = SemanticStep(
                id=plan.id,
                goal=plan.goal,
                rationale=plan.rationale,
                expected_result=plan.expected_result,
                intended_scope=plan.intended_scope,
                validation_requirements=plan.validation_requirements,
            )
            tools = self._tools()
            decision = self._decide(step, plan, tools)
            if decision is None:
                reason = self._decision_error or "provider returned no usable decision"
                self._decision_error = None
                if self._failover("controller", reason):
                    continue  # retry the same decision on the fallback model
                outcome = self._recover(f"provider output: {reason}", plan)
                if outcome is RecoveryAction.CONTINUE:
                    continue
                if outcome is RecoveryAction.UNAVAILABLE:
                    self.state.status = "failed: provider output"
                    self.state.phase = RunPhase.FAILED
                    self._persist()
                return self.state

            if decision.action is Action.ASK_USER:
                self.state.pending_question = decision.reason_summary
                self.state.status = "waiting_for_user"
                self._event("user_question", payload={"question": decision.reason_summary})
                self._persist()
                return self.state

            if decision.action is Action.REPLAN:
                if self._replan(plan, decision.reason_summary, failed=False):
                    stopped = self._handle_stagnation(decision.reason_summary)
                    if stopped is not None:
                        return stopped
                continue

            if decision.action is Action.FINISH_CANDIDATE:
                if self._verify_and_route("finish candidate failed verification"):
                    return self.state
                continue

            if decision.action is Action.COMPLETE_SEMANTIC_STEP:
                if self._evaluate_step(plan, tools):
                    return self.state
                continue

            if decision.tool is None:
                raise RuntimeError("execute_tool decision omitted tool")

            if self.repetition.seen(decision.tool.name, decision.tool.arguments):
                self._event(
                    "repetition_detected",
                    RunPhase.EXECUTE,
                    step_id=plan.id,
                    payload={"tool": decision.tool.name},
                )
                if self._evaluate_step(plan, tools):
                    return self.state
                continue

            result = tools.execute(decision.tool)
            self._save_tool_result(result, plan.id)
            self._observe(result, decision.reason_summary)
            self.state.step_tool_calls += 1
            self._persist()

            if self.state.step_tool_calls >= self.max_tool_calls_per_step:
                self._event(
                    "step_budget_exhausted",
                    RunPhase.EXECUTE,
                    step_id=plan.id,
                    payload={"tool_calls": self.state.step_tool_calls},
                )
                if self._evaluate_step(plan, tools):
                    return self.state

    # ------------------------------------------------------------------ steps

    def _step_budget(self) -> int:
        """Iterations available to this run() call, including budget won by recovery."""
        return self.max_steps + self._recovery_extensions

    def _decide(
        self,
        step: SemanticStep,
        plan: PlanStep,
        tools: ToolRegistry,
    ) -> Decision | None:
        assert self.state is not None and self.repo is not None and self.memory is not None
        context = ContextBuilder(self.memory).build(
            self.state,
            step,
            validation=self.state.latest_validation,
            budget=self.context_limit,
            current_diff=self.repo.diff(),
            active_files=self.repo.changed_files(),
            observations=self.state.latest_observations,
            working=self.state.working_memory,
            step_tool_calls=self.state.step_tool_calls,
            max_tool_calls=self.max_tool_calls_per_step,
        )
        self.last_context_info = {
            "characters": len(context.text),
            "estimated_tokens": estimate_tokens(context.text),
            "pinned": len(context.pinned_ids),
            "omitted": len(context.omitted_ids),
        }
        self.last_context_text = context.text
        self._event(
            "context_built",
            RunPhase.EXECUTE,
            step_id=plan.id,
            payload=dict(self.last_context_info),
        )
        try:
            controller_provider = self.provider_for("controller")
            with self._progress("controller", controller_provider):
                decision = Controller(
                    DecisionProvider(controller_provider), tools.names()
                ).decide(context.text)
        except ProviderOutputError as exc:
            # not fatal yet: run() lets the recovery advisor read the trace first
            logger.error("provider failure: %s", exc)
            self._remember("failure", f"provider failure: {exc}", immutable=True)
            self._decision_error = str(exc)
            return None
        self._event(
            "decision",
            RunPhase.EXECUTE,
            step_id=plan.id,
            payload=decision.model_dump(mode="json"),
        )
        return decision

    def _evaluate_step(self, plan: PlanStep, tools: ToolRegistry) -> bool:
        """Run deterministic validation plus evaluation; return True if the run ended."""
        assert self.state is not None and self.repo is not None
        self.repo.clean_generated_artifacts()
        self.repo.clean_ignored_artifacts()
        if self.repo.worktree_merge_in_progress():
            # the agent edits conflicted files directly; staging is the runtime's job
            self.repo.stage_all()
        self._transition(RunPhase.VALIDATE)
        validation = DeterministicValidator(
            self.repo,
            tools,
            self.validator_commands,
            self.scope_warning_files,
        ).validate(plan.intended_scope)
        self.state.latest_validation = validation
        self._save_diff(plan.id)
        self._event(
            "validation",
            RunPhase.VALIDATE,
            step_id=plan.id,
            payload=validation.model_dump(mode="json"),
        )
        payload = EvaluationInput(
            objective=self.state.objective,
            semantic_goal=plan.goal,
            hard_constraints=self.state.hard_constraints,
            latest_observations=self.state.latest_observations[-5:],
            accepted_commit=self.state.accepted_commit,
            context=self._evaluation_context(plan, validation),
            validation=validation,
        )
        self._transition(RunPhase.EVALUATE)
        evaluation: Evaluation | None = None
        for attempt in (1, 2):
            try:
                with self._progress("evaluator", getattr(self.evaluator, "provider", None)):
                    evaluation = self.evaluator.evaluate(payload)
                break
            except ProviderOutputError as exc:
                logger.error("evaluator failure: %s", exc)
                self._remember("failure", f"evaluator failure: {exc}", immutable=True)
                if attempt == 1 and self._failover("evaluator", str(exc)):
                    continue  # judge the same work again, on the fallback model
                outcome = self._recover(f"evaluator output: {exc}", plan)
                if outcome is RecoveryAction.CONTINUE:
                    return False
                if outcome is RecoveryAction.UNAVAILABLE:
                    self.state.status = "failed: evaluator output"
                    self.state.phase = RunPhase.FAILED
                    self._persist()
                return True
        assert evaluation is not None
        self._promote(evaluation.memories_to_promote)
        self._event(
            "evaluation",
            RunPhase.EVALUATE,
            step_id=plan.id,
            payload=evaluation.model_dump(mode="json"),
        )

        if evaluation.decision == "finish_candidate":
            return self._verify_and_route("finish candidate failed verification")
        if evaluation.decision == "replan":
            if self._replan(plan, evaluation.reason, failed=False):
                if self._handle_stagnation(evaluation.reason) is not None:
                    return True
            return False
        if evaluation.decision == "continue":
            self._transition(RunPhase.EXECUTE)
            self._persist()
            return False
        if evaluation.decision == "accept":
            if validation.changed_files or self.repo.worktree_merge_in_progress():
                self._transition(RunPhase.CHECKPOINT)
                self.state.accepted_commit = self.repo.checkpoint(f"gcae: {plan.goal}")
                logger.info(
                    "checkpoint %s for step %s", self.state.accepted_commit[:12], plan.id
                )
                self.state.accepted_steps += 1
                self._consecutive_failures = 0
                self._event(
                    "checkpoint_created",
                    RunPhase.CHECKPOINT,
                    step_id=plan.id,
                    payload={
                        "commit": self.state.accepted_commit,
                        "message": f"gcae: {plan.goal}",
                        "kind": "step",
                    },
                )
            else:
                logger.info("step %s accepted without file changes", plan.id)
            plan.status = "completed"
            self.state.plan = [item for item in self.state.plan if item.id != plan.id]
            self._settle_working_memory(plan)
            self._event(
                "step_accepted",
                RunPhase.CHECKPOINT,
                step_id=plan.id,
                payload={
                    "goal": plan.goal,
                    "commit": self.state.accepted_commit,
                    "changed_files": list(validation.changed_files),
                    "accepted_steps": self.state.accepted_steps,
                    "remaining_steps": len(self.state.plan),
                },
            )
            self._emit_candidate_state()
            # an accepted step is progress by definition: the evaluator judged it
            # worthwhile and the plan advanced. Only rejected attempts and replans
            # signal stagnation (see _handle_stagnation).
            self.stagnation.record(True)
            if validation.changed_files or self.repo.worktree_merge_in_progress():
                self._clear_failure_streak()
            self._persist()
            return False

        self._remember("failure", evaluation.reason, immutable=True)
        if self._record_failure(evaluation.reason):
            self._event(
                "repeated_failure",
                RunPhase.ROLLBACK,
                step_id=plan.id,
                payload={
                    "signature": evaluation.reason,
                    "count": self._same_failure_count,
                },
            )
            # The same rejection three times means the *approach* is exhausted, not just the
            # attempt: give the step back to a stronger model before asking the user.
            if self._failover(
                "controller",
                f"the same failure repeated {self._same_failure_count} times: {evaluation.reason}",
            ):
                self._clear_failure_streak()
            elif self._handle_stagnation(
                f"the same failure repeated {self._same_failure_count} times: {evaluation.reason}"
            ) is not None:
                return True
        if self._conflict_resolution:
            # the merge is in progress: rolling back would lose the conflict context
            self._fail_conflict_resolution([plan.goal])
            return True
        self._rollback(evaluation.reason)
        if self._replan(plan, evaluation.reason, failed=True):
            if self._handle_stagnation(evaluation.reason) is not None:
                return True
        return False

    def _record_failure(self, reason: str) -> bool:
        """True when the same failure has now repeated ``repeated_failure_limit`` times.

        Consecutive identical rejections are stagnation by another name: the run is spending
        iterations without changing the outcome. Counting the signature catches that even when
        an unrelated step is accepted in between, which would otherwise keep resetting the
        stagnation window.
        """
        signature = reason.strip()
        if signature and signature == self._last_failure_signature:
            self._same_failure_count += 1
        else:
            self._last_failure_signature = signature
            self._same_failure_count = 1
        return self._same_failure_count >= self.repeated_failure_limit

    def _clear_failure_streak(self) -> None:
        self._last_failure_signature = ""
        self._same_failure_count = 0

    def _escalate(self, reason: str) -> None:
        self._escalated = True
        self._event("model_escalated", RunPhase.PLAN, payload={"reason": reason})
        logger.warning("escalating to the configured stronger model: %s", reason)

    def _handle_stagnation(self, reason: str) -> AgentState | None:
        """Stagnation means "change strategy", not "give up".

        Order of responses: tell the next attempt not to repeat the failed approach,
        escalate to the configured stronger model, then ask the user. Only a run that was
        already asked and still makes no progress fails, and its accepted checkpoints stay
        intact (the caller delivers them).
        """
        assert self.state is not None
        self._remember(
            "decision",
            f"stagnation: {reason} — the previous approach is exhausted; change hypothesis",
        )
        if not self._stagnation_escalated and "escalation" in self.role_providers:
            self._stagnation_escalated = True
            self._escalate(reason)
            return None
        outcome = self._recover(f"stagnation: {reason}", self._current_plan_step())
        if outcome is RecoveryAction.CONTINUE:
            return None
        if outcome is not RecoveryAction.UNAVAILABLE:
            # the advisor asked the user or declared the task impossible itself
            return self.state
        if not self._stagnation_asked:
            self._stagnation_asked = True
            committed = (
                f" latest checkpoint {self.state.accepted_commit[:7]}"
                if self.state.accepted_commit
                else " no checkpoint yet"
            )
            question = (
                f"no verified progress after {self.stagnation.window} attempts ({reason}). "
                f"accepted steps: {self.state.accepted_steps},{committed}. "
                "Tell me how to proceed: send an instruction, or stop the run."
            )
            self.state.pending_question = question
            self.state.status = "waiting_for_user"
            self._event("user_question", payload={"question": question, "reason": reason})
            self._persist()
            logger.warning("run %s is waiting for the user: %s", self.state.run_id, reason)
            return self.state
        return self._fail(f"execution stagnated after asking: {reason}")

    # ------------------------------------------------------------------ liveness

    def _progress(self, role: str, provider: object | None) -> AbstractContextManager[None]:
        """Report a provider call as events, so a slow model never looks frozen.

        Providers that can stream expose ``on_progress``; the listener turns their updates
        into ``provider_started`` / ``provider_first_token`` / ``provider_progress`` /
        ``provider_waiting`` events, rate limited here so a fast stream cannot flood the
        log or the dashboard.
        """
        if provider is None or not hasattr(provider, "on_progress"):
            return nullcontext()
        return _ProgressReporter(self, role, provider)

    # ------------------------------------------------------------------ recovery

    def _failover(self, role: str, reason: str) -> bool:
        """Move a broken role onto the configured fallback model before giving up on it.

        A provider outage is the one failure the recovery advisor cannot reason its way out of:
        the advisor would have to call the same broken endpoint. When ``models.escalation`` is
        configured it becomes the fallback for the role that failed, once per role, and the
        run continues on that model instead of ending.
        """
        fallback = self.role_providers.get("escalation")
        if fallback is None or role in self._failed_over:
            return False
        target: object | None = None
        if role == "controller":
            self._escalate(reason)  # sets the flag provider_for("controller") reads
            target = fallback
        elif role == "planner":
            # PlannerLike is a protocol; only a model-backed planner has a provider to swap
            planner: Any = self.planner
            target = getattr(planner, "provider", None)
            if target is not None:
                planner.provider = fallback
        elif role == "evaluator":
            target = getattr(self.evaluator, "provider", None)
            if target is not None:
                self.evaluator.provider = fallback  # type: ignore[attr-defined]
        elif role == "verifier":
            target = getattr(self.verifier, "judge", None)
            if target is not None:
                self.verifier.judge = fallback
        if target is None:
            return False
        self._failed_over.add(role)
        model = str(getattr(fallback, "model", fallback.__class__.__name__))
        self._remember("failure", f"{role} provider failed, falling back to {model}: {reason}")
        self._event(
            "model_failover",
            self.state.phase if self.state else None,
            payload={"role": role, "model": model, "reason": reason},
        )
        logger.warning("%s failed (%s); continuing on %s", role, reason, model)
        return True

    def _recovery_provider(self) -> Provider:
        """The advisor runs on [models.recovery], else on the controller's model (which is
        the escalated one after escalation)."""
        return self.role_providers.get("recovery") or self.provider_for("controller")

    def _recover(self, trigger: str, plan: PlanStep | None) -> RecoveryAction:
        """Read this run's own trace and try to correct it before giving up.

        The advisor sees the persisted record — state, filtered events, validation and
        verification evidence, failure memories — not the controller's working context, so it
        can question an assumption the controller keeps repeating. Bounded by
        ``runtime.recovery_attempts``; a failed or unusable diagnosis returns
        ``UNAVAILABLE`` and leaves the caller's own ladder intact.
        """
        assert self.state is not None and self.memory is not None
        if self._recoveries >= self.recovery_attempts:
            logger.warning("no recovery attempts left for %s", trigger)
            return RecoveryAction.UNAVAILABLE
        self._recoveries += 1
        self._event(
            "recovery_started",
            self.state.phase,
            payload={"trigger": trigger, "attempt": self._recoveries},
        )
        try:
            return self._diagnose(trigger, plan)
        except Exception as exc:  # noqa: BLE001 - a broken rescue must not kill the patient
            self._degrade("recovery", exc)
            self._remember(
                "failure",
                f"recovery itself failed: {type(exc).__name__}: {exc}",
                immutable=True,
            )
            return RecoveryAction.UNAVAILABLE

    def _diagnose(self, trigger: str, plan: PlanStep | None) -> RecoveryAction:
        """The advisor call, separated so every failure inside it is contained."""
        assert self.state is not None and self.memory is not None
        provider = self._recovery_provider()
        model = str(getattr(provider, "model", provider.__class__.__name__))
        logger.warning("recovery %d: diagnosing %s", self._recoveries, trigger)
        try:
            with self._progress("recovery", provider):
                diagnosis = RecoveryAdvisor(provider).diagnose(self._recovery_trace())
        except ProviderOutputError as exc:
            logger.error("recovery diagnosis unavailable: %s", exc)
            self._remember("failure", f"recovery diagnosis unavailable: {exc}", immutable=True)
            self._event(
                "recovery_failed",
                self.state.phase,
                payload={"trigger": trigger, "error": str(exc)},
            )
            self._persist()
            return RecoveryAction.UNAVAILABLE
        record = RecoveryRecord(
            attempt=self._recoveries,
            trigger=trigger,
            root_cause=diagnosis.root_cause,
            corrective_instruction=diagnosis.corrective_instruction,
            strategy=diagnosis.strategy,
            model=model,
        )
        self.state.recovery = record
        self._remember(
            "decision",
            f"recovery: {diagnosis.root_cause} — next attempt: "
            f"{diagnosis.corrective_instruction}",
        )
        self._event(
            "recovery_completed",
            self.state.phase,
            payload=record.model_dump(mode="json"),
        )
        logger.warning(
            "recovery %d: %s -> %s", self._recoveries, diagnosis.root_cause,
            diagnosis.corrective_instruction,
        )
        if diagnosis.success_criteria and not self.state.success_criteria:
            # the planner failed to state what "done" means: the advisor's criteria are the
            # only checkable definition available, so the run can still be verified
            self.state.success_criteria = list(diagnosis.success_criteria)
            self._event(
                "success_criteria_adopted",
                self.state.phase,
                payload={"criteria": list(diagnosis.success_criteria), "source": "recovery"},
            )
            self._remember(
                "decision",
                "criteria adopted from recovery: " + "; ".join(diagnosis.success_criteria),
            )
        if diagnosis.strategy == "ask_user":
            question = (
                f"recovery could not fix the run: {diagnosis.root_cause}. "
                f"Suggested next step: {diagnosis.corrective_instruction} "
                "Tell me how to proceed, or stop the run."
            )
            self.state.pending_question = question
            self.state.status = "waiting_for_user"
            self._event("user_question", payload={"question": question, "reason": trigger})
            self._persist()
            return RecoveryAction.ASK_USER
        if diagnosis.strategy == "stop":
            self._fail(f"recovery advised stopping: {diagnosis.root_cause}")
            return RecoveryAction.FAILED
        self.stagnation.reset()
        self._recovery_extensions += self.recovery_budget
        self._queue_correction(plan, diagnosis.corrective_instruction)
        return RecoveryAction.CONTINUE

    def _queue_correction(self, plan: PlanStep | None, instruction: str) -> None:
        """Discard speculative work and queue the step the advisor prescribed."""
        assert self.state is not None
        if self.repo is not None and self.repo.worktree is not None and self.repo.status():
            self._rollback(f"recovery: {instruction}")
        plan = plan or self._current_plan_step()
        if plan is not None:
            plan.status = "failed"
            self.state.plan = [item for item in self.state.plan if item.id != plan.id]
        self.state.plan.extend(next_step(self.state, instruction))
        self._reset_working_memory()
        self.repetition = RepetitionGuard(self.repetition_limit)
        self.state.working_memory.hypotheses.append(f"recovery: {instruction}")
        self._enter_plan()
        self._emit_plan(reason=f"recovery: {instruction}", replaced=plan.id if plan else None,
                        failed=True)
        self._event(
            "replan",
            RunPhase.PLAN,
            step_id=plan.id if plan else None,
            payload={"reason": f"recovery: {instruction}"},
        )
        self._persist()

    def _enter_plan(self) -> None:
        """Move to PLAN from wherever recovery found the run, through legal transitions.

        Recovery can fire immediately after an accepted step (the budget is checked after the
        checkpoint), and neither ``checkpoint -> plan`` nor ``checkpoint -> rollback`` is legal:
        the run used to die with an "unexpected error" raised by its own recovery handler.
        CHECKPOINT therefore goes through EXECUTE, which the state machine allows.
        """
        assert self.state is not None
        phase = self.state.phase
        if phase in _DIRECT_TO_PLAN:
            self._transition(RunPhase.PLAN)
            return
        if phase is RunPhase.CHECKPOINT:
            self._transition(RunPhase.EXECUTE)
            self._transition(RunPhase.PLAN)
            return
        # FAILED is terminal for the state machine; recovery may reopen a run that failed while
        # it was already diagnosing, exactly as `resume` reopens one from disk.
        self.state.phase = RunPhase.PLAN
        self.state.updated_at = now_utc()

    def _recovery_trace(self) -> str:
        assert self.state is not None and self.memory is not None
        memories = [
            record
            for record in self.memory.recent(self.state.run_id, limit=60)
            if record.kind in {"failure", "decision", "user_instruction"}
        ]
        candidate = ""
        if self.repo is not None and self.repo.worktree is not None:
            try:
                snapshot = self.repo.candidate_snapshot()
            except GitError:
                snapshot = {}
            if snapshot:
                files = [entry["path"] for entry in snapshot.get("files", [])]
                candidate = (
                    f"{len(files)} changed file(s) "
                    f"(+{snapshot.get('added', 0)} -{snapshot.get('deleted', 0)}): "
                    f"{', '.join(files[:8])}"
                )
        return build_trace(self.state, list(self._trace), memories, candidate=candidate)

    def _replan(self, plan: PlanStep, reason: str, failed: bool) -> bool:
        """Discard speculative work and the current step, queue a new one.

        Returns True when stagnation is detected.
        """
        assert self.state is not None
        if self.repo is not None and self.repo.worktree is not None and self.repo.status():
            self._rollback()
        if failed:
            self._consecutive_failures += 1
            if (
                self._consecutive_failures >= 2
                and not self._escalated
                and "escalation" in self.role_providers
            ):
                self._escalate(reason)
        self._remember("decision", f"replan: {reason}")
        if plan.status in {"pending", "active"}:
            plan.status = "failed" if failed else "skipped"
        self.state.plan = [item for item in self.state.plan if item.id != plan.id]
        self.state.plan.extend(next_step(self.state, reason))
        self._reset_working_memory()
        self.repetition = RepetitionGuard(self.repetition_limit)
        self.state.working_memory.hypotheses.append(f"retry after: {reason}")
        self._transition(RunPhase.PLAN)
        self._emit_plan(reason=reason, replaced=plan.id, failed=failed)
        self._event("replan", RunPhase.PLAN, step_id=plan.id, payload={"reason": reason})
        stagnated = self.stagnation.record(False)
        if stagnated:
            logger.error("stagnation detected: %s", reason)
        self._persist()
        return stagnated

    # ------------------------------------------------------------------ verification

    def _verify_and_route(self, failure_reason: str) -> bool:
        assert self.state is not None and self.repo is not None
        self.repo.clean_generated_artifacts()
        self.repo.clean_ignored_artifacts()
        if self.repo.worktree_merge_in_progress():
            self.repo.stage_all()
        unresolved = self.repo.conflict_marker_files()
        if unresolved:
            # never checkpoint conflict markers as if they were the verified result
            details = f"unresolved merge conflicts: {', '.join(unresolved)}"
            logger.warning("verification blocked: %s", details)
            if self._conflict_resolution:
                self._fail_conflict_resolution(unresolved)
                return True
            self._remember("failure", details)
            self._rollback(details)
            self.state.plan.extend(next_step(self.state, details))
            self._transition(RunPhase.PLAN)
            self._persist()
            return False
        self._transition(RunPhase.VERIFY)
        self._event("verification_started", RunPhase.VERIFY)
        with self._progress("verifier", getattr(self.verifier, "judge", None)):
            report = self.verifier.verify(self.state, diff=self.repo.diff())
        # criterion commands may generate caches; never let them reach the checkpoint
        self.repo.clean_generated_artifacts()
        self.repo.clean_ignored_artifacts()
        self.state.last_verification = report
        self._event(
            "verification_completed",
            RunPhase.VERIFY,
            payload=report.model_dump(mode="json"),
        )
        self._emit_candidate_state()
        if report.passed and report.hygiene_passed:
            if self.repo.status():
                self._transition(RunPhase.CHECKPOINT)
                self.state.accepted_commit = self.repo.checkpoint("gcae: verified final state")
                logger.info("final checkpoint %s", self.state.accepted_commit[:12])
                self.state.accepted_steps += 1
                self._event(
                    "checkpoint_created",
                    RunPhase.CHECKPOINT,
                    payload={
                        "commit": self.state.accepted_commit,
                        "message": "gcae: verified final state",
                        "kind": "verification",
                    },
                )
            for plan in self.state.plan:
                plan.status = "completed"
            self.state.plan = []
            self.state.status = "complete"
            self._transition(RunPhase.COMPLETE)
            self._persist()
            logger.info(
                "run %s complete with %d accepted steps",
                self.state.run_id,
                self.state.accepted_steps,
            )
            self._event("run_completed", RunPhase.COMPLETE)
            return True
        missing = report.missing_requirements
        details = "; ".join([*missing, *report.details]) or "criteria or hygiene did not pass"
        logger.warning("run %s verification failed: %s", self.state.run_id, details)
        self._remember("failure", f"{failure_reason}: {details}", immutable=True)
        self.state.plan.extend(next_step(self.state, failure_reason))
        self._transition(RunPhase.PLAN)
        self._persist()
        return False

    # ------------------------------------------------------------------ control

    def _pump_control(self) -> bool:
        control = self.control
        state = self.state
        if control is None or state is None:
            return False
        while True:
            for text in control.take_instructions():
                if text.strip():
                    self.inject_user_instruction(text.strip())
            if control.stopped:
                state.status = "stopped"
                self._event("run_stopped")
                self._persist()
                logger.warning("run stopped by user")
                return True
            if not control.paused:
                return False
            time.sleep(0.05)

    # ------------------------------------------------------------------ helpers

    def _current_plan_step(self) -> PlanStep | None:
        assert self.state is not None
        return next(
            (step for step in self.state.plan if step.status in {"pending", "active"}),
            None,
        )

    def _tools(self) -> ToolRegistry:
        assert self.state is not None
        return ToolRegistry(
            self.state.worktree,
            self.command_timeout,
            artifact_dir=self._run_dir() / "artifacts",
            test_commands=self.validator_commands,
        )

    def _observe(self, result: ToolResult, reason: str) -> None:
        assert self.state is not None
        observation = Observation(
            tool=result.tool,
            summary=(result.output.strip() or result.error or reason)[:500],
            artifact=result.artifact,
        )
        text = observation.summary
        self._remember("observation", text)
        self.state.latest_observations.append(text)
        self.state.latest_observations = self.state.latest_observations[-20:]
        working = self.state.working_memory
        if not result.success and result.error:
            working.blocker = result.error
        else:
            working.blocker = None
        working.findings.append(f"{observation.tool}: {text}"[:500])
        working.findings = working.findings[-8:]
        for path in result.changed_files:
            if path not in working.active_files:
                working.active_files.append(path)
        working.active_files = working.active_files[-12:]
        self._event(
            "tool_result",
            RunPhase.EXECUTE,
            step_id=self.state.current_step_id,
            payload=result.model_dump(mode="json"),
        )
        self._emit_candidate_state()

    def _settle_working_memory(self, plan: PlanStep) -> None:
        assert self.state is not None
        self._remember("decision", f"step {plan.id} completed: {plan.goal}")
        self._reset_working_memory()

    def _reset_working_memory(self) -> None:
        assert self.state is not None
        self.state.working_memory = WorkingMemory()

    def _rollback(self, reason: str = "") -> None:
        assert self.state is not None and self.repo is not None
        if self.state.phase != RunPhase.ROLLBACK:
            self._transition(RunPhase.ROLLBACK)
        target = self.state.accepted_commit or self.repo.current_commit()
        source = self.repo.current_commit()
        try:
            discarded = [entry["path"] for entry in self.repo.candidate_snapshot()["files"]]
        except GitError:
            discarded = []
        logger.warning("rollback to %s", target[:12])
        try:
            self.repo.rollback(target)
        except GitError as exc:
            # the trusted state could not be restored: record it and let the caller's ladder
            # decide. The accepted commits are still on the branch, so no work is lost.
            self._remember("failure", f"rollback failed: {exc}", immutable=True)
            self._event(
                "rollback_failed",
                RunPhase.ROLLBACK,
                step_id=self.state.current_step_id,
                payload={"target": target, "error": str(exc)},
            )
            self._persist()
            raise
        self._event(
            "rollback_completed",
            RunPhase.ROLLBACK,
            step_id=self.state.current_step_id,
            payload={
                "from_commit": source,
                "to_commit": target,
                "reason": reason,
                "discarded": discarded,
            },
        )
        self._emit_candidate_state()

    def _fail(self, reason: str, *, persist: bool = True) -> AgentState:
        assert self.state is not None
        logger.error("run %s failed: %s", self.state.run_id, reason)
        self.state.status = f"failed: {reason}"
        self.state.phase = RunPhase.FAILED
        self._event("run_failed", RunPhase.FAILED, payload={"reason": reason})
        self._persist(required=persist)
        return self.state

    def _evaluation_context(self, plan: PlanStep, validation: ValidationResult) -> str:
        assert self.state is not None and self.repo is not None and self.memory is not None
        step = SemanticStep(
            id=plan.id,
            goal=plan.goal,
            rationale=plan.rationale,
            expected_result=plan.expected_result,
            intended_scope=plan.intended_scope,
            validation_requirements=plan.validation_requirements,
        )
        result = ContextBuilder(self.memory).build(
            self.state,
            step,
            validation=validation,
            budget=self.context_limit,
            current_diff=self.repo.diff(),
            active_files=self.repo.changed_files(),
            observations=self.state.latest_observations,
            working=self.state.working_memory,
        )
        return result.text

    def _promote(self, candidates: list[MemoryCandidate]) -> None:
        assert self.state is not None and self.memory is not None
        for candidate in candidates:
            record = candidate.record.model_copy(
                update={
                    "id": None,
                    "run_id": self.state.run_id,
                    "step_id": self.state.current_step_id,
                    "commit_sha": self.state.accepted_commit,
                    "source": "evaluator",
                }
            )
            self.memory.add(record)
        if candidates:
            self._emit_memory_counts()

    def release_lock(self) -> None:
        """Give up the per-repository run lock (idempotent)."""
        if self._run_lock is not None:
            self._run_lock.release()
            self._run_lock = None

    def _run_dir(self) -> Path:
        assert self.state is not None
        return self.runtime_dir / "runs" / self.state.run_id

    def _save_tool_result(self, result: ToolResult, step_id: str) -> None:
        assert self.state is not None
        safe_step = re.sub(r"[^A-Za-z0-9._-]+", "_", step_id)
        directory = self._run_dir() / "tool-results"
        directory.mkdir(parents=True, exist_ok=True)
        name = f"{self.state.iteration:04d}-{self.state.step_tool_calls:02d}-{safe_step}.json"
        path = directory / name
        path.write_text(
            result.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )

    def _save_diff(self, step_id: str) -> None:
        assert self.state is not None and self.repo is not None
        safe_step = re.sub(r"[^A-Za-z0-9._-]+", "_", step_id)
        directory = self._run_dir() / "diffs"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.state.iteration:04d}-{safe_step}.diff"
        path.write_text(self.repo.diff(), encoding="utf-8")

    def _degrade(self, what: str, exc: BaseException) -> None:
        """Record that a non-essential subsystem failed, without ending the run.

        State, memory and events are how the runtime explains itself; losing one of them is
        serious and is reported (a ``runtime_degraded`` event, a bounded list in ``state.json``,
        the CLI summary and a WARNING), but it is never a reason to abort work that the
        repository still holds. Recovery keeps working because it tolerates all three.
        """
        message = f"{what} failed: {type(exc).__name__}: {exc}"
        logger.warning("run degraded — %s", message)
        state = self.state
        if state is None:
            return
        if message not in state.degradations and len(state.degradations) < 10:
            state.degradations.append(message)
        if self._degrade_notified.get(what):
            return
        self._degrade_notified[what] = True
        try:
            self._event(
                "runtime_degraded", state.phase, payload={"component": what, "error": message}
            )
        except Exception:  # noqa: BLE001 - the reporting path is already degraded
            logger.debug("could not record the degradation event", exc_info=True)

    def _persist(self, *, required: bool = True) -> None:
        """Write ``state.json``; failure to do so stops the run.

        Memory and events degrade, state does not: a stale ``state.json`` makes a run *look*
        resumable from a checkpoint the branch has already moved past.  ``required=False`` is
        for the last-resort paths that are already reporting a terminal state.
        """
        if self.state is None:
            return
        self.state.updated_at = now_utc()
        try:
            StateStore(self._run_dir() / "state.json").save(self.state)
        except Exception as exc:  # noqa: BLE001 - the reason is what matters, not the type
            self._degrade("state file", exc)
            if not required:
                return
            raise UnresumableStateError(
                f"run state could not be written: {type(exc).__name__}: {exc} — stopping so "
                "resume cannot continue from a stale checkpoint (accepted commits are intact)"
            ) from exc

    def _remember(self, kind: str, content: str, immutable: bool = False) -> None:
        if self.state is None or self.memory is None:
            return
        try:
            self.memory.add(
                MemoryRecord(
                    kind=kind,
                    content=content,
                    run_id=self.state.run_id,
                    step_id=self.state.current_step_id,
                    commit_sha=self.state.accepted_commit,
                    immutable=immutable,
                )
            )
        except Exception as exc:  # noqa: BLE001 - knowledge is lost, the run is not
            self._degrade("memory store", exc)
            return
        self._emit_memory_counts()

    def resolve_merge_conflicts(self) -> list[str]:
        """Let the agent resolve a merge conflict inside its own worktree, then re-verify.

        Returns the conflicting paths handed to the agent (empty when there was nothing to
        resolve). The merge happens inside the run's worktree — never in the user's
        checkout — so a failure is aborted and rolled back, leaving the branch as it was.
        """
        if self.state is None or self.repo is None:
            raise RuntimeError("call start or resume before resolving conflicts")
        if not self.auto_resolve_conflicts:
            return []
        state = self.state
        if not state.branch or state.merge is not None:
            return []
        self.repo.ensure_worktree(state.branch, Path(state.worktree))
        target = self.repo.current_branch()
        files = self.repo.merge_into_worktree(target)
        if not files:
            # the branch already contains the target; the outer merge is a fast-forward now
            return []
        self._event(
            "conflict_detected",
            RunPhase.PLAN,
            payload={"target": target, "files": files},
        )
        logger.warning("merge conflict in %s; handing it to the agent", ", ".join(files))
        step = PlanStep(
            id=f"step-{state.next_step_number}",
            goal=f"resolve the merge conflict in {', '.join(files)}",
            rationale=f"merging {target} into the run branch conflicts in those files",
            expected_result=(
                "conflict markers removed, both sides' intent preserved, "
                "the original success criteria still pass"
            ),
            intended_scope=list(files),
            validation_requirements=list(state.success_criteria),
        )
        state.next_step_number += 1
        state.plan.append(step)
        state.status = "running"
        state.phase = RunPhase.PLAN
        state.current_step_id = step.id
        state.step_tool_calls = 0
        self.repetition = RepetitionGuard(self.repetition_limit)
        self._persist()
        previous_status, previous_phase = state.status, state.phase
        self._conflict_failed = False
        self._conflict_resolution = True
        try:
            self.run()
        finally:
            self._conflict_resolution = False
        if self._conflict_failed or self.repo.worktree_merge_in_progress():
            # the agent did not finish: leave the branch exactly as it was, and keep the
            # run's own outcome (the merge is what failed, not the verified work)
            self._fail_conflict_resolution(files, previous_status, previous_phase)
            return []
        self._event(
            "conflict_resolved",
            RunPhase.COMPLETE,
            payload={"files": files, "commit": state.accepted_commit},
        )
        return files

    def _fail_conflict_resolution(
        self,
        files: list[str],
        previous_status: str = "complete",
        previous_phase: RunPhase = RunPhase.COMPLETE,
    ) -> None:
        """Give up on a merge conflict: abort it and leave the branch exactly as it was."""
        assert self.state is not None and self.repo is not None
        state = self.state
        if self.repo.worktree_merge_in_progress():
            self.repo.abort_worktree_merge()
        if state.accepted_commit:
            try:
                self.repo.rollback(state.accepted_commit)
            except GitError:  # pragma: no cover - best effort cleanup
                logger.exception("rollback after an unresolved conflict failed")
        self._conflict_failed = True
        state.plan = [step for step in state.plan if not step.id.startswith("step-")]
        state.status = previous_status
        state.phase = previous_phase
        self._remember(
            "failure",
            f"merge conflict unresolved in {', '.join(files)}; the run's own work is unchanged",
        )
        self._event("conflict_unresolved", RunPhase.FAILED, payload={"files": files})
        self._persist()

    def merge_completed_run(self, allow_unverified: bool = False) -> MergeRecord:
        """Merge the verified run branch into the source branch (recorded, reversible).

        Only a completed run whose branch still points at the verified commit is merged;
        the source repository must be clean, which ``merge_branch`` enforces. The merge is
        recorded in ``state.json`` so ``gcae undo`` can reverse it.
        """
        if self.state is None or self.repo is None:
            raise RuntimeError("call start or resume before merging a run")
        record = merge_verified_run(
            self.repo, self.state, persist=self._persist, allow_unverified=allow_unverified
        )
        self._event(
            "merge_completed",
            RunPhase.COMPLETE,
            payload=record.model_dump(mode="json"),
        )
        self._publish_repository_notices()
        if self.cleanup_after_merge and cleanup_merged_worktree(self.repo, self.state):
            self._event(
                "worktree_cleaned",
                RunPhase.COMPLETE,
                payload={"worktree": self.state.worktree, "branch": self.state.branch},
            )
        logger.info(
            "merged %s into %s (%s -> %s)",
            record.branch,
            record.target_branch,
            record.pre_merge_commit[:12],
            record.merge_commit[:12],
        )
        return record

    def _publish_repository_notices(self) -> None:
        """Report every Git precondition GCAE repaired on its own behalf."""
        if self.repo is None:
            return
        for notice in self.repo.notices:
            self._event(
                "repository_notice",
                self.state.phase if self.state is not None else None,
                payload=dict(notice),
            )
            logger.info("repository notice: %s", notice.get("message"))
        self.repo.notices.clear()

    def _emit_plan(
        self,
        reason: str,
        replaced: str | None = None,
        failed: bool = False,
    ) -> None:
        assert self.state is not None
        self._event(
            "plan_updated",
            self.state.phase,
            payload={
                "reason": reason,
                "steps": [
                    {"id": step.id, "goal": step.goal, "status": step.status}
                    for step in self.state.plan
                ],
                "completed": self.state.accepted_steps,
                "replaced": replaced,
                "failed": failed,
            },
        )

    def _emit_candidate_state(self) -> None:
        """Publish the exact candidate state without making the UI run git itself."""
        if self.state is None or self.repo is None or self.repo.worktree is None:
            return
        try:
            snapshot = self.repo.candidate_snapshot()
        except GitError:
            return
        snapshot["accepted_commit"] = self.state.accepted_commit
        self._event("candidate_state", self.state.phase, payload=snapshot)

    def _emit_memory_counts(self) -> None:
        if self.state is None or self.memory is None:
            return
        try:
            counts = self.memory.counts(self.state.run_id)
        except sqlite3.Error:
            return
        self._event("memory_updated", self.state.phase, payload={"counts": counts})

    def _event(
        self,
        event_type: str,
        phase: RunPhase | None = None,
        step_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self.state is None or self.events is None:
            return
        event = Event(
            run_id=self.state.run_id,
            event_type=event_type,
            phase=phase,
            step_id=step_id,
            payload=payload or {},
        )
        self._trace.append(event)
        try:
            self.events.append(event)
        except Exception as exc:  # noqa: BLE001 - a full disk must not kill the run
            # bookkeeping is useful, not essential: the run continues and reports that its
            # own record is incomplete instead of dying because a log could not be written
            self._degrade("event log", exc)
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - a broken subscriber must not stop the run
                logger.exception("event subscriber failed")

    def _transition(self, phase: RunPhase) -> None:
        assert self.state is not None
        if self.state.phase != phase:
            self.machine.transition(self.state, phase)
            self._event(f"phase:{phase.value}", phase)


class _ProgressReporter:
    """Context manager attaching (and detaching) one provider progress listener."""

    def __init__(self, runtime: Runtime, role: str, provider: object) -> None:
        self.runtime = runtime
        self.role = role
        self.provider: Any = provider
        self.last_emit = 0.0
        self.last_characters = -1
        self.last_heartbeat = 0.0
        self.announced = False
        self.model = ""
        self.started = 0.0
        self.progress = StreamProgress(characters=0)
        self._stop = threading.Event()
        self._beats: threading.Thread | None = None

    def __enter__(self) -> None:
        self.model = str(getattr(self.provider, "model", self.provider.__class__.__name__))
        self.started = time.monotonic()
        self._beats = threading.Thread(
            target=self._beat, name=f"gcae-heartbeat-{self.role}", daemon=True
        )
        self._beats.start()
        self.runtime._event(
            "provider_started",
            self.runtime.state.phase if self.runtime.state else None,
            payload={"role": self.role, "model": self.model},
        )
        self.provider.on_progress = self._report

    def __exit__(self, *exc_info: object) -> None:
        self.provider.on_progress = None
        self._stop.set()
        if self._beats is not None:
            self._beats.join(timeout=1.0)
        self.runtime._event(
            "provider_finished",
            self.runtime.state.phase if self.runtime.state else None,
            payload={
                "role": self.role,
                "model": self.model,
                "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                "characters": self.progress.characters,
                "reasoning_characters": self.progress.reasoning_characters,
            },
        )

    def _beat(self) -> None:
        """Emit a heartbeat while the call is silent, whatever the provider can report."""
        interval = max(0.05, PROVIDER_HEARTBEAT_SECONDS)
        while not self._stop.wait(interval):
            runtime = self.runtime
            if runtime.state is None:
                continue
            runtime._event(
                "provider_waiting",
                runtime.state.phase,
                payload={
                    "role": self.role,
                    "characters": self.progress.characters,
                    "reasoning_characters": self.progress.reasoning_characters,
                    "elapsed_ms": int((time.monotonic() - self.started) * 1000),
                },
            )

    def _report(self, progress: StreamProgress) -> None:
        runtime = self.runtime
        if runtime.state is None:
            return
        self.progress = progress
        now = time.monotonic()
        payload = {
            "role": self.role,
            "characters": progress.characters,
            "reasoning_characters": progress.reasoning_characters,
            "elapsed_ms": progress.elapsed_ms,
        }
        if progress.waiting:
            # a heartbeat: the stream is open but nothing arrived. Keeps the log and the
            # dashboard moving during a long deliberation instead of showing a dead screen.
            if now - self.last_heartbeat < 10.0:
                return
            self.last_heartbeat = now
            runtime._event("provider_waiting", runtime.state.phase, payload=payload)
            return
        if not self.announced:
            self.announced = True
            runtime._event("provider_first_token", runtime.state.phase, payload=payload)
        if now - self.last_emit < 0.4 and progress.characters - self.last_characters < 2048:
            return
        self.last_emit = now
        self.last_characters = progress.characters
        runtime._event(
            "provider_progress",
            runtime.state.phase,
            payload={**payload, "preview": progress.preview},
        )
