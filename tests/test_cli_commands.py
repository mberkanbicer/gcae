import json
from pathlib import Path

from gcae.cli import _inspect_run, _list_runs
from gcae.models import AgentState
from gcae.persistence import StateStore


def make_state(run_id: str, updated: str) -> AgentState:
    state = AgentState(
        run_id=run_id,
        source_repo="/source",
        worktree="/worktree",
        branch=f"gcae/{run_id}",
        objective=f"objective {run_id}",
        original_request=f"objective {run_id}",
        success_criteria=["file exists: result.txt"],
        accepted_steps=2,
        status="complete",
    )
    state.updated_at = state.updated_at.fromisoformat(updated)
    return state


def test_list_and_inspect_runs(tmp_path: Path, capsys) -> None:
    runtime_dir = tmp_path / "runtime"
    StateStore(runtime_dir / "runs" / "run-a" / "state.json").save(
        make_state("run-a", "2026-01-01T10:00:00+00:00")
    )
    StateStore(runtime_dir / "runs" / "run-b" / "state.json").save(
        make_state("run-b", "2026-01-02T10:00:00+00:00")
    )
    _list_runs(runtime_dir)
    output = capsys.readouterr().out
    assert "run-a" in output
    assert "run-b" in output
    assert "objective run-b" in output
    assert output.index("run-b") < output.index("run-a")

    _inspect_run("run-a", runtime_dir, as_json=True)
    payload = json.loads(capsys.readouterr().out)
    assert payload["run_id"] == "run-a"
    assert payload["accepted_steps"] == 2

    _inspect_run("run-a", runtime_dir, as_json=False)
    summary = capsys.readouterr().out
    assert "objective run-a" in summary
    assert "criteria: file exists: result.txt" in summary


def test_list_runs_reports_empty_state(tmp_path: Path, capsys) -> None:
    _list_runs(tmp_path / "runtime")
    assert "no runs found" in capsys.readouterr().out
