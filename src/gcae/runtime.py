from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .context import ContextBuilder, estimate_tokens
from .controller import Controller
from .evaluator import DeterministicEvaluator, Evaluator
from .git import GitRepository
from .memory import EventLog, MemoryStore
from .models import (
    Action,
    AgentState,
    Decision,
    EvaluationInput,
    Event,
    MemoryCandidate,
    MemoryRecord,
    Observation,
    PlanStep,
    RunPhase,
    SemanticStep,
    ToolResult,
    ValidationResult,
    WorkingMemory,
    now_utc,
)
from .persistence import StateStore
from .planner import Planner, PlannerLike, next_step
from .providers import DecisionProvider, FakeProvider, Provider, ProviderOutputError
from .safeguards import RepetitionGuard, StagnationDetector
from .state_machine import StateMachine
from .tools import ToolRegistry
from .validation import DeterministicValidator
from .verifier import FinalVerifier

logger = logging.getLogger("gcae")

EventSubscriber = Callable[[Event], None]


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
        self._consecutive_failures = 0
        self._escalated = False
        self.last_context_info: dict[str, int] = {}

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

    def start(
        self,
        request: str,
        hard_constraints: list[str] | None = None,
        success_criteria: list[str] | None = None,
        run_id: str | None = None,
    ) -> AgentState:
        run_id = run_id or uuid.uuid4().hex[:12]
        self.repo = GitRepository(self.source_repo, self.runtime_dir, self.worktree_dir)
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
        try:
            initial_plan = self.planner.plan(self.state)
        except ProviderOutputError as exc:
            self.state.status = "failed: planner output"
            self.state.phase = RunPhase.FAILED
            self._remember("failure", f"planner failure: {exc}", immutable=True)
            self._persist()
            raise RuntimeError(f"planner failed: {exc}") from exc
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
        self._remember("user_instruction", request, immutable=True)
        for constraint in self.state.hard_constraints:
            self._remember("user_instruction", f"hard constraint: {constraint}", immutable=True)
        for criterion in self.state.success_criteria:
            self._remember("user_instruction", f"success criterion: {criterion}", immutable=True)
        self._event("run_started", RunPhase.ANALYZE, payload={"objective": self.state.objective})
        self._persist()
        return self.state

    def resume(self, run_id: str) -> AgentState:
        path = self.runtime_dir / "runs" / run_id
        self.state = StateStore(path / "state.json").load()
        if Path(self.state.source_repo).resolve() != self.source_repo:
            raise RuntimeError("resume source repository does not match persisted state")
        self.repo = GitRepository(self.source_repo, self.runtime_dir, self.worktree_dir)
        self.repo.worktree = Path(self.state.worktree)
        self.repo.branch = self.state.branch
        self.memory = MemoryStore(self.runtime_dir / "memory.db")
        self.events = EventLog(path / "events.jsonl")
        self.repo.validate_source()
        if not self.repo.worktree.exists():
            raise RuntimeError(f"persisted agent worktree does not exist: {self.repo.worktree}")
        self.repo.assert_registered_worktree()
        if self.state.accepted_commit:
            self.repo.rollback(self.state.accepted_commit)
        if self.state.status != "complete":
            self.state.status = "running"
            self.state.phase = RunPhase.PLAN
            self.state.pending_question = None
            self.state.step_tool_calls = 0
            self._persist()
        self._event("run_resumed", self.state.phase, payload={"run_id": run_id})
        return self.state

    def inject_user_instruction(self, text: str) -> AgentState:
        """Record a user override, discard speculative work and replan."""
        if self.state is None:
            raise RuntimeError("call start or resume before injecting an instruction")
        self.state.latest_user_instruction = text
        self.state.pending_question = None
        self._remember("user_instruction", f"user override: {text}", immutable=True)
        if self.repo is not None and self.repo.worktree is not None and self.repo.status():
            self._rollback()
        for plan in self.state.plan:
            if plan.status in {"pending", "active"}:
                plan.status = "skipped"
        self.state.plan = [plan for plan in self.state.plan if plan.status != "skipped"]
        self.state.plan.extend(next_step(self.state, f"user override: {text}"))
        self._reset_working_memory()
        self._transition(RunPhase.PLAN)
        self._event("user_override", RunPhase.PLAN, payload={"text": text})
        logger.info("user override: %s", text)
        self._persist()
        return self.state

    # ------------------------------------------------------------------ main loop

    def run(self) -> AgentState:
        if self.state is None:
            raise RuntimeError("call start or resume before run")
        if self.state.status == "complete":
            return self.state
        assert self.repo is not None and self.memory is not None

        for _ in range(self.max_steps):
            if self._pump_control():
                return self.state
            self.state.iteration += 1
            plan = self._current_plan_step()
            if plan is None:
                if self._verify_and_route("final verification failed"):
                    return self.state
                continue

            self.state.current_step_id = plan.id
            if plan.status == "pending":
                plan.status = "active"
                self.state.step_tool_calls = 0
                self.repetition = RepetitionGuard(self.repetition_limit)
                self.state.working_memory.pending_validations = list(
                    plan.validation_requirements
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
                return self.state

            if decision.action is Action.ASK_USER:
                self.state.pending_question = decision.reason_summary
                self.state.status = "waiting_for_user"
                self._event("user_question", payload={"question": decision.reason_summary})
                self._persist()
                return self.state

            if decision.action is Action.REPLAN:
                if self._replan(plan, decision.reason_summary, failed=False):
                    return self._fail("execution stagnated")
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

        if self.repo.status():
            self._rollback()
        return self._fail("step budget exhausted")

    # ------------------------------------------------------------------ steps

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
        try:
            decision = Controller(
                DecisionProvider(self.provider_for("controller")), tools.names()
            ).decide(context.text)
        except ProviderOutputError as exc:
            logger.error("provider failure: %s", exc)
            self._remember("failure", f"provider failure: {exc}", immutable=True)
            self.state.status = "failed: provider output"
            self.state.phase = RunPhase.FAILED
            self._persist()
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
        try:
            evaluation = self.evaluator.evaluate(payload)
        except ProviderOutputError as exc:
            logger.error("evaluator failure: %s", exc)
            self._remember("failure", f"evaluator failure: {exc}", immutable=True)
            self.state.status = "failed: evaluator output"
            self.state.phase = RunPhase.FAILED
            self._persist()
            return True
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
                self._fail("execution stagnated")
                return True
            return False
        if evaluation.decision == "continue":
            self._transition(RunPhase.EXECUTE)
            self._persist()
            return False
        if evaluation.decision == "accept":
            if validation.changed_files:
                self._transition(RunPhase.CHECKPOINT)
                self.state.accepted_commit = self.repo.checkpoint(f"gcae: {plan.goal}")
                logger.info(
                    "checkpoint %s for step %s", self.state.accepted_commit[:12], plan.id
                )
                self.state.accepted_steps += 1
                self._consecutive_failures = 0
            else:
                logger.info("step %s accepted without file changes", plan.id)
            plan.status = "completed"
            self.state.plan = [item for item in self.state.plan if item.id != plan.id]
            self._settle_working_memory(plan)
            if self.stagnation.record(bool(validation.changed_files)):
                self._fail("execution stagnated")
                return True
            self._persist()
            return False

        self._remember("failure", evaluation.reason, immutable=True)
        self._rollback()
        if self._replan(plan, evaluation.reason, failed=True):
            self._fail("execution stagnated")
            return True
        return False

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
                self._escalated = True
                self._event("model_escalated", RunPhase.PLAN, payload={"reason": reason})
                logger.warning("escalating to the configured stronger model: %s", reason)
        self._remember("decision", f"replan: {reason}")
        if plan.status in {"pending", "active"}:
            plan.status = "failed" if failed else "skipped"
        self.state.plan = [item for item in self.state.plan if item.id != plan.id]
        self.state.plan.extend(next_step(self.state, reason))
        self._reset_working_memory()
        self.repetition = RepetitionGuard(self.repetition_limit)
        self.state.working_memory.hypotheses.append(f"retry after: {reason}")
        self._transition(RunPhase.PLAN)
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
        self._transition(RunPhase.VERIFY)
        self._event("verification_started", RunPhase.VERIFY)
        report = self.verifier.verify(self.state, diff=self.repo.diff())
        self.state.last_verification = report
        self._event(
            "verification_completed",
            RunPhase.VERIFY,
            payload=report.model_dump(mode="json"),
        )
        if report.passed and report.hygiene_passed:
            if self.repo.status():
                self._transition(RunPhase.CHECKPOINT)
                self.state.accepted_commit = self.repo.checkpoint("gcae: verified final state")
                logger.info("final checkpoint %s", self.state.accepted_commit[:12])
                self.state.accepted_steps += 1
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

    def _settle_working_memory(self, plan: PlanStep) -> None:
        assert self.state is not None
        self._remember("decision", f"step {plan.id} completed: {plan.goal}")
        self._reset_working_memory()

    def _reset_working_memory(self) -> None:
        assert self.state is not None
        self.state.working_memory = WorkingMemory()

    def _rollback(self) -> None:
        assert self.state is not None and self.repo is not None
        if self.state.phase != RunPhase.ROLLBACK:
            self._transition(RunPhase.ROLLBACK)
        target = self.state.accepted_commit or self.repo.current_commit()
        logger.warning("rollback to %s", target[:12])
        self.repo.rollback(target)
        self._event("rollback_completed", RunPhase.ROLLBACK, step_id=self.state.current_step_id)

    def _fail(self, reason: str) -> AgentState:
        assert self.state is not None
        logger.error("run %s failed: %s", self.state.run_id, reason)
        self.state.status = f"failed: {reason}"
        self.state.phase = RunPhase.FAILED
        self._event("run_failed", RunPhase.FAILED, payload={"reason": reason})
        self._persist()
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

    def _persist(self) -> None:
        assert self.state is not None
        self.state.updated_at = now_utc()
        StateStore(self._run_dir() / "state.json").save(self.state)

    def _remember(self, kind: str, content: str, immutable: bool = False) -> None:
        assert self.state is not None and self.memory is not None
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

    def _event(
        self,
        event_type: str,
        phase: RunPhase | None = None,
        step_id: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        assert self.state is not None and self.events is not None
        event = Event(
            run_id=self.state.run_id,
            event_type=event_type,
            phase=phase,
            step_id=step_id,
            payload=payload or {},
        )
        self.events.append(event)
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
