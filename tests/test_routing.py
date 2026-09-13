import subprocess
from pathlib import Path

import pytest

from gcae.cli import _planner, _providers, _verifier
from gcae.config import (
    Config,
    ModelOverride,
    ModelsConfig,
    PlannerConfig,
    ProviderConfig,
    VerifierConfig,
)
from gcae.evaluator import LLMEvaluator
from gcae.http_provider import OpenAICompatibleProvider
from gcae.models import Evaluation, EvaluationInput
from gcae.planner import LLMPlanner, Planner
from gcae.providers import FakeProvider
from gcae.runtime import Runtime
from gcae.verifier import FinalVerifier


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "T"], check=True)
    (path / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


class RecordingProvider:
    def __init__(self, outputs: list[dict[str, object]]) -> None:
        self.inner = FakeProvider(outputs)
        self.calls = 0

    def complete(self, prompt, schema):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.inner.complete(prompt, schema)


class SequenceEvaluator:
    def __init__(self, decisions: list[str]) -> None:
        self.decisions = list(decisions)
        self.calls = 0

    def evaluate(self, payload: EvaluationInput) -> Evaluation:
        del payload
        self.calls += 1
        decision = self.decisions.pop(0) if self.decisions else "accept"
        return Evaluation(decision=decision, reason=f"call {self.calls}")


def step(action: str) -> dict[str, object]:
    return {"action": action, "semantic_goal": "step", "reason_summary": "step"}


def create(path: str, content: str) -> dict[str, object]:
    return {
        "action": "execute_tool",
        "semantic_goal": "create",
        "reason_summary": "create",
        "tool": {"name": "create_file", "arguments": {"path": path, "content": content}},
    }


def test_role_models_resolve_from_config() -> None:
    config = Config(
        provider=ProviderConfig(kind="http", base_url="http://local/v1", model="base"),
        models=ModelsConfig(evaluator=ModelOverride(model="judge")),
    )
    base, roles = _providers(config)
    assert isinstance(base, OpenAICompatibleProvider)
    assert base.model == "base"
    assert isinstance(roles["evaluator"], OpenAICompatibleProvider)
    assert roles["evaluator"].model == "judge"
    assert "controller" not in roles
    base.close()
    roles["evaluator"].close()


def test_planner_kind_resolution() -> None:
    provider = FakeProvider([])
    assert isinstance(_planner(Config(), provider), Planner)
    assert isinstance(
        _planner(Config(provider=ProviderConfig(kind="http")), provider), LLMPlanner
    )
    assert isinstance(_planner(Config(planner=PlannerConfig(kind="llm")), provider), LLMPlanner)
    with pytest.raises(ValueError):
        _planner(Config(planner=PlannerConfig(kind="bogus")), provider)


def test_verifier_kind_resolution() -> None:
    provider = FakeProvider([])
    assert _verifier(Config(), provider).judge is None
    assert _verifier(Config(verifier=VerifierConfig(kind="hybrid")), provider).judge is provider
    with pytest.raises(ValueError):
        _verifier(Config(verifier=VerifierConfig(kind="bogus")), provider)


def test_llm_planner_merges_user_criteria(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    plan = {
        "objective": "refined objective",
        "success_criteria": ["file exists: inferred.txt"],
        "hard_constraints": ["no new dependencies"],
        "assumptions": ["repository is python"],
        "steps": [
            {
                "id": "step-7",
                "goal": "create inferred.txt",
                "rationale": "requested",
                "expected_result": "file exists",
                "intended_scope": [],
                "validation_requirements": [],
            }
        ],
    }
    provider = FakeProvider([plan])
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, planner=LLMPlanner(provider))
    state = runtime.start("do the thing", success_criteria=["file exists: user.txt"])
    assert state.objective == "refined objective"
    assert state.success_criteria == ["file exists: user.txt", "file exists: inferred.txt"]
    assert state.hard_constraints == ["no new dependencies"]
    assert state.assumptions == ["repository is python"]
    assert [item.id for item in state.plan] == ["step-7"]
    assert state.next_step_number == 8


def test_planner_failure_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider([{"action": "nonsense"}], repair_limit=0)
    runtime = Runtime(source, tmp_path / "runtime", provider=provider, planner=LLMPlanner(provider))
    with pytest.raises(RuntimeError, match="planner failed"):
        runtime.start("do the thing")


def test_escalation_after_repeated_failures(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    controller = FakeProvider(
        [
            create("a.txt", "a"),
            step("complete_semantic_step"),
            create("b.txt", "b"),
            step("complete_semantic_step"),
        ]
    )
    escalation = RecordingProvider([create("c.txt", "c"), step("complete_semantic_step")])
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=controller,
        evaluator=SequenceEvaluator(["rollback", "rollback", "accept"]),
        role_providers={"escalation": escalation},
        max_steps=10,
    )
    runtime.start("create c.txt", success_criteria=["file exists: c.txt"])
    result = runtime.run()
    assert result.status == "complete"
    assert escalation.calls > 0
    worktree = Path(result.worktree)
    assert (worktree / "c.txt").exists()
    assert not (worktree / "a.txt").exists()
    assert not (worktree / "b.txt").exists()


def test_hybrid_verification_completes_with_natural_language_criterion(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider(
        [
            create("done.txt", "ok"),
            step("complete_semantic_step"),
        ]
    )
    judge = FakeProvider(
        [{"passed": True, "evidence": "done.txt was created in the worktree"}]
    )
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        verifier=FinalVerifier(judge),
    )
    runtime.start("create done.txt", success_criteria=["the requested file was created"])
    result = runtime.run()
    assert result.status == "complete"
    assert result.last_verification is not None
    assert result.last_verification.passed


def test_evaluator_output_failure_is_reported(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    provider = FakeProvider([create("a.txt", "a"), step("complete_semantic_step")])
    runtime = Runtime(
        source,
        tmp_path / "runtime",
        provider=provider,
        evaluator=LLMEvaluator(FakeProvider([], repair_limit=0)),
    )
    runtime.start("create a.txt", success_criteria=["file exists: a.txt"])
    result = runtime.run()
    assert result.status.startswith("failed: evaluator output")
