from pathlib import Path

import pytest

from gcae.models import AgentState, Decision, RunPhase
from gcae.persistence import StateStore
from gcae.providers import FakeProvider, ProviderOutputError
from gcae.state_machine import StateMachine


def make_state() -> AgentState:
    return AgentState(
        run_id="r",
        source_repo="/src",
        worktree="/wt",
        branch="b",
        objective="o",
        original_request="o",
    )


def test_state_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = make_state()
    store.save(state)
    assert store.load() == state


def test_state_machine_explicit_transitions() -> None:
    state = make_state()
    machine = StateMachine()
    machine.transition(state, RunPhase.PLAN)
    with pytest.raises(ValueError):
        machine.transition(state, RunPhase.COMPLETE)


def test_fake_provider_repairs_once() -> None:
    provider = FakeProvider([{"action": "execute", "tool": {"name": "read_file"}}])
    decision = provider.complete("", Decision)
    assert decision.action == "execute_tool"
    assert provider.repairs == 1


def test_fake_provider_bounded_failure() -> None:
    provider = FakeProvider([{"action": "unknown"}], repair_limit=0)
    with pytest.raises(ProviderOutputError):
        provider.complete("", Decision)
