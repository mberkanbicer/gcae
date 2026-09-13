from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

from .context import ContextBuilder
from .controller import Controller
from .evaluator import Evaluator
from .git import GitRepository
from .memory import EventLog, MemoryStore
from .models import (
    Action,
    AgentState,
    Event,
    MemoryRecord,
    PlanStep,
    RunPhase,
    SemanticStep,
    ToolResult,
    now_utc,
)
from .persistence import StateStore
from .planner import Planner
from .providers import DecisionProvider, FakeProvider, Provider, ProviderOutputError
from .safeguards import RepetitionGuard, StagnationDetector
from .state_machine import StateMachine
from .tools import ToolRegistry
from .validation import DeterministicValidator
from .verifier import FinalVerifier


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
        evaluator: Evaluator | None = None,
    ) -> None:
        self.source_repo = Path(source_repo).resolve()
        self.runtime_dir = Path(runtime_dir).expanduser().resolve()
        self.worktree_dir = (
            Path(worktree_dir).expanduser().resolve()
            if worktree_dir is not None
            else None
        )
        self.provider = provider or FakeProvider(
            [{"action": "finish", "semantic_goal": "finish", "reason_summary": "offline"}]
        )
        self.validator_commands = list(validator_commands or [])
        self.max_steps = max_steps
        self.command_timeout = command_timeout
        self.evaluator = evaluator or Evaluator()
        self.planner = Planner()
        self.machine = StateMachine()
        self.verifier = FinalVerifier()
        self.state: AgentState | None = None
        self.repo: GitRepository | None = None
        self.memory: MemoryStore | None = None
        self.events: EventLog | None = None
        self.repetition = RepetitionGuard()
        self.stagnation = StagnationDetector()

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
        self.memory = MemoryStore(self.runtime_dir / "runs" / run_id / "memory.db")
        self.events = EventLog(self.runtime_dir / "runs" / run_id / "events.jsonl")
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
        self.state.plan = self.planner.plan(self.state)
        self._transition(RunPhase.PLAN)
        self._remember("user_instruction", request, immutable=True)
        for constraint in self.state.hard_constraints:
            self._remember(
                "user_instruction",
                f"hard constraint: {constraint}",
                immutable=True,
            )
        for criterion in self.state.success_criteria:
            self._remember(
                "user_instruction",
                f"success criterion: {criterion}",
                immutable=True,
            )
        self._event("run_started", RunPhase.ANALYZE)
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
        self.memory = MemoryStore(path / "memory.db")
        self.events = EventLog(path / "events.jsonl")
        self.repo.validate_source()
        if not self.repo.worktree.exists():
            raise RuntimeError(f"persisted agent worktree does not exist: {self.repo.worktree}")
        if self.state.accepted_commit:
            self.repo.rollback(self.state.accepted_commit)
        if self.state.status != "complete":
            self.state.status = "running"
            self.state.phase = RunPhase.PLAN
            self._persist()
        return self.state

    def run(self) -> AgentState:
        if self.state is None:
            raise RuntimeError("call start or resume before run")
        if self.state.status == "complete":
            return self.state
        assert self.repo is not None and self.memory is not None

        for _ in range(self.max_steps):
            self.state.iteration += 1
            plan = self._current_plan_step()
            if plan is None:
                if self._verify_and_route("final verification failed"):
                    return self.state
                continue

            self.state.current_step_id = plan.id
            self._transition(RunPhase.EXECUTE)
            step = SemanticStep(
                id=plan.id,
                goal=plan.goal,
                rationale=plan.rationale,
                expected_result=plan.expected_result,
                intended_scope=plan.intended_scope,
                validation_requirements=plan.validation_requirements,
            )
            context = ContextBuilder(self.memory).build(
                self.state,
                step,
                budget=8192,
                current_diff=self.repo.diff(),
                active_files=self.repo.changed_files(),
                observations=self.state.latest_observations,
            )
            try:
                decision = Controller(DecisionProvider(self.provider)).decide(context.text)
            except ProviderOutputError as exc:
                self._remember("failure", f"provider failure: {exc}", immutable=True)
                self.state.status = "failed: provider output"
                self.state.phase = RunPhase.FAILED
                self._persist()
                return self.state
            self._event(
                "decision",
                RunPhase.EXECUTE,
                step_id=plan.id,
                payload=decision.model_dump(mode="json"),
            )

            if decision.action is Action.ASK_USER:
                self.state.status = "waiting_for_user"
                self._persist()
                return self.state
            if decision.action is Action.CONTINUE:
                self._remember("observation", decision.reason_summary)
                self._record_observation(decision.reason_summary)
                self._persist()
                continue
            if decision.action is Action.REPLAN:
                self._remember("observation", decision.reason_summary)
                if self.repo.status():
                    self._rollback()
                self._transition(RunPhase.PLAN)
                self.state.plan = self.planner.replan(self.state, decision.reason_summary)
                self._persist()
                continue
            if decision.action is Action.FINISH:
                if self._verify_and_route("finish candidate failed verification"):
                    return self.state
                continue
            if decision.tool is None:
                raise RuntimeError("execute_tool decision omitted tool")

            if self.repetition.seen(decision.tool.name, decision.tool.arguments):
                reason = "repeated identical tool action"
                self._remember("failure", reason, immutable=True)
                if self.repo.status():
                    self._rollback()
                self._transition(RunPhase.PLAN)
                self.state.plan = self.planner.replan(self.state, reason)
                self._persist()
                continue

            tools = ToolRegistry(self.state.worktree, self.command_timeout)
            result = tools.execute(decision.tool)
            self._save_tool_result(result, plan.id)
            observation = result.output.strip() or result.error or decision.reason_summary
            self._remember("observation", observation)
            self._record_observation(observation)
            self._transition(RunPhase.VALIDATE)
            self.repo.clean_generated_artifacts()
            self.repo.clean_ignored_artifacts()
            validation = DeterministicValidator(
                self.repo, tools, self.validator_commands
            ).validate(plan.intended_scope)
            self._save_diff(plan.id)
            self._event(
                "validation",
                RunPhase.VALIDATE,
                step_id=plan.id,
                payload=validation.model_dump(mode="json"),
            )
            evaluation = self.evaluator.evaluate(decision, validation)
            if not result.success:
                evaluation = evaluation.model_copy(
                    update={"outcome": "rollback", "reason": result.error or "tool failed"}
                )
            self._event(
                "evaluation",
                RunPhase.EVALUATE,
                step_id=plan.id,
                payload=evaluation.model_dump(mode="json"),
            )

            if evaluation.outcome == "continue":
                self._transition(RunPhase.EVALUATE)
                self._transition(RunPhase.EXECUTE)
                self._persist()
                continue
            if evaluation.outcome == "finish_candidate":
                self._transition(RunPhase.EVALUATE)
                if self._verify_and_route("finish candidate failed verification"):
                    return self.state
                continue
            if evaluation.outcome == "replan":
                self._remember("observation", evaluation.reason)
                self._rollback()
                self._transition(RunPhase.PLAN)
                self.state.plan = self.planner.replan(self.state, evaluation.reason)
                self._persist()
                continue
            if evaluation.outcome == "accept":
                self._transition(RunPhase.EVALUATE)
                if not validation.changed_files:
                    if self.stagnation.record(False):
                        return self._fail("execution stagnated")
                    self._persist()
                    continue
                self._transition(RunPhase.CHECKPOINT)
                self.state.accepted_commit = self.repo.checkpoint(f"gcae: {plan.goal}")
                self.state.accepted_steps += 1
                plan.status = "accepted"
                self.state.plan = [item for item in self.state.plan if item.id != plan.id]
                self.stagnation.record(True)
                self._persist()
                continue

            self._remember("failure", evaluation.reason, immutable=True)
            self._rollback()
            if self.stagnation.record(False):
                return self._fail("execution stagnated")
            self._transition(RunPhase.PLAN)
            self.state.plan = self.planner.replan(self.state, evaluation.reason)
            self._persist()

        if self.repo.status():
            self._rollback()
        return self._fail("step budget exhausted")

    def _current_plan_step(self) -> PlanStep | None:
        assert self.state is not None
        return next((step for step in self.state.plan if step.status == "pending"), None)

    def _verify_and_route(self, failure_reason: str) -> bool:
        assert self.state is not None and self.repo is not None
        self.repo.clean_generated_artifacts()
        self.repo.clean_ignored_artifacts()
        self._transition(RunPhase.VERIFY)
        report = self.verifier.verify(self.state)
        self.state.last_verification = report
        if report.passed and report.hygiene_passed:
            self.state.status = "complete"
            self._transition(RunPhase.COMPLETE)
            self._persist()
            return True
        details = "; ".join(report.details) or "criteria or hygiene did not pass"
        self._remember("failure", f"{failure_reason}: {details}", immutable=True)
        self._transition(RunPhase.PLAN)
        self.state.plan = self.planner.replan(self.state, failure_reason)
        self._persist()
        return False

    def _rollback(self) -> None:
        assert self.state is not None and self.repo is not None
        if self.state.phase != RunPhase.ROLLBACK:
            self._transition(RunPhase.ROLLBACK)
        self.repo.rollback(self.state.accepted_commit or self.repo.current_commit())
        self._event("rollback_completed", RunPhase.ROLLBACK, step_id=self.state.current_step_id)

    def _fail(self, reason: str) -> AgentState:
        assert self.state is not None
        self.state.status = f"failed: {reason}"
        self.state.phase = RunPhase.FAILED
        self._persist()
        return self.state

    def _record_observation(self, observation: str) -> None:
        assert self.state is not None
        self.state.latest_observations.append(observation)
        self.state.latest_observations = self.state.latest_observations[-20:]

    def _run_dir(self) -> Path:
        assert self.state is not None
        return self.runtime_dir / "runs" / self.state.run_id

    def _save_tool_result(self, result: ToolResult, step_id: str) -> None:
        assert self.state is not None
        safe_step = re.sub(r"[^A-Za-z0-9._-]+", "_", step_id)
        directory = self._run_dir() / "tool-results"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.state.iteration:04d}-{safe_step}.json"
        path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _save_diff(self, step_id: str) -> None:
        assert self.state is not None
        assert self.repo is not None
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
        self.events.append(
            Event(
                run_id=self.state.run_id,
                event_type=event_type,
                phase=phase,
                step_id=step_id,
                payload=payload or {},
            )
        )

    def _transition(self, phase: RunPhase) -> None:
        assert self.state is not None
        if self.state.phase != phase:
            self.machine.transition(self.state, phase)
            self._event(f"phase:{phase.value}", phase)
