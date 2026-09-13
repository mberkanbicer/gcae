from gcae.models import AgentState, RunPhase


def test_scaffold_contract() -> None:
    state = AgentState(
        run_id="r",
        source_repo="/src",
        worktree="/wt",
        branch="b",
        objective="o",
        original_request="o",
    )
    assert state.phase is RunPhase.ANALYZE
