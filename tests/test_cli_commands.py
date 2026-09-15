import json
from pathlib import Path

import pytest

from gcae.cli import _inspect_run, _list_runs, _prune_runs
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


def test_version_reports_the_installed_distribution() -> None:
    """`gcae --version` must come from packaging metadata, never a hardcoded string."""
    import importlib.metadata

    from gcae.cli import _version

    assert _version() == importlib.metadata.version("gcae")


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


def test_headless_run_requires_a_request(tmp_path: Path, capsys) -> None:
    from gcae.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(["run", str(tmp_path / "repo"), "--headless"])
    assert exit_info.value.code == 1
    assert "request is required" in capsys.readouterr().err


# ------------------------------------------------------------ prune (run-data retention)


def test_prune_keeps_the_newest_runs_and_deletes_the_rest(tmp_path: Path, capsys) -> None:
    runtime_dir = tmp_path / "runtime"
    for index in range(12):
        stamp = f"2026-01-{index + 1:02d}T10:00:00+00:00"
        StateStore(runtime_dir / "runs" / f"run-{index:02d}" / "state.json").save(
            make_state(f"run-{index:02d}", stamp)
        )
    # the whole run directory goes, not just state.json
    (runtime_dir / "runs" / "run-00" / "events.jsonl").write_text("{}\n")

    _prune_runs(runtime_dir, keep=10, dry_run=False)
    output = capsys.readouterr().out
    assert "pruned run-00" in output and "pruned run-01" in output
    assert "2 run record(s) pruned" in output
    assert not (runtime_dir / "runs" / "run-00").exists(), "the whole directory must go"
    remaining = sorted(path.name for path in (runtime_dir / "runs").glob("run-*"))
    assert remaining == [f"run-{index:02d}" for index in range(2, 12)]


def test_prune_dry_run_lists_but_deletes_nothing(tmp_path: Path, capsys) -> None:
    runtime_dir = tmp_path / "runtime"
    for index in range(3):
        stamp = f"2026-01-{index + 1:02d}T10:00:00+00:00"
        StateStore(runtime_dir / "runs" / f"run-{index:02d}" / "state.json").save(
            make_state(f"run-{index:02d}", stamp)
        )
    _prune_runs(runtime_dir, keep=1, dry_run=True)
    output = capsys.readouterr().out
    assert "would prune run-00" in output and "would prune run-01" in output
    assert "2 run record(s) would be pruned" in output
    assert (runtime_dir / "runs" / "run-00" / "state.json").exists()
    assert (runtime_dir / "runs" / "run-01" / "state.json").exists()


def test_prune_never_deletes_a_run_whose_repository_lock_is_held(
    tmp_path: Path, capsys
) -> None:
    """An old record whose repository is locked belongs to a live run in another process."""
    import subprocess
    import sys
    import time

    runtime_dir = tmp_path / "runtime"
    repo = tmp_path / "repo"
    for index, run_id in enumerate(("run-old", "run-mid", "run-new")):
        state = make_state(run_id, f"2026-01-{index + 1:02d}T10:00:00+00:00")
        if run_id == "run-old":
            state.source_repo = str(repo)  # only the old record shares the locked repository
        StateStore(runtime_dir / "runs" / run_id / "state.json").save(state)
    marker = tmp_path / "locked"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time\n"
                "from pathlib import Path\n"
                "sys.path.insert(0, sys.argv[1])\n"
                "from gcae.runtime import RunLock\n"
                "lock = RunLock(Path(sys.argv[2]), Path(sys.argv[3]), 'run old-run')\n"
                "lock.acquire()\n"
                "Path(sys.argv[4]).write_text('locked')\n"
                "time.sleep(30)\n"
            ),
            str(Path(__file__).resolve().parents[1] / "src"),
            str(runtime_dir),
            str(repo),
            str(marker),
        ]
    )
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the helper process never took the lock"

        _prune_runs(runtime_dir, keep=1, dry_run=False)
        stdout, stderr = capsys.readouterr()
        assert "keeping run-old" in stderr, "a locked repository means a live run"
        assert "pruned run-mid" in stdout, "an unlocked old record is pruned"
        assert (runtime_dir / "runs" / "run-old").exists()
        assert not (runtime_dir / "runs" / "run-mid").exists()
        assert (runtime_dir / "runs" / "run-new").exists(), "the newest is always kept"
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_prune_with_no_runs_reports_empty(tmp_path: Path, capsys) -> None:
    _prune_runs(tmp_path / "runtime", keep=10, dry_run=False)
    assert "no runs found" in capsys.readouterr().out


def test_the_prune_command_reaches_the_cli(tmp_path: Path, capsys) -> None:
    from gcae.cli import main

    runtime_dir = tmp_path / "runtime"
    StateStore(runtime_dir / "runs" / "run-a" / "state.json").save(
        make_state("run-a", "2026-01-01T10:00:00+00:00")
    )
    StateStore(runtime_dir / "runs" / "run-b" / "state.json").save(
        make_state("run-b", "2026-01-02T10:00:00+00:00")
    )
    main(["prune", "--runtime-dir", str(runtime_dir), "--keep", "1"])
    output = capsys.readouterr()
    assert "pruned run-a" in output.out
    assert not (runtime_dir / "runs" / "run-a").exists()
    assert (runtime_dir / "runs" / "run-b").exists()


def test_prune_keeps_a_record_with_an_un_undone_merge(tmp_path: Path, capsys) -> None:
    """`gcae undo` reads the pre-merge/merge pair from the run record; deleting it silently
    would break that. Only --force overrides the guard."""
    from gcae.models import MergeRecord

    runtime_dir = tmp_path / "runtime"
    for index, run_id in enumerate(("run-old", "run-new")):
        state = make_state(run_id, f"2026-01-{index + 1:02d}T10:00:00+00:00")
        if run_id == "run-old":
            state.merge = MergeRecord(
                branch="gcae/run-old",
                target_branch="main",
                pre_merge_commit="aaaa1111",
                merge_commit="bbbb2222",
            )
        StateStore(runtime_dir / "runs" / run_id / "state.json").save(state)

    _prune_runs(runtime_dir, keep=1, dry_run=False)
    output = capsys.readouterr()
    assert "keeping run-old" in output.err
    assert "its merge is still recorded" in output.err
    assert "nothing to prune" in output.out
    assert (runtime_dir / "runs" / "run-old").exists()

    _prune_runs(runtime_dir, keep=1, dry_run=False, force=True)
    assert "pruned run-old" in capsys.readouterr().out
    assert not (runtime_dir / "runs" / "run-old").exists()
    assert (runtime_dir / "runs" / "run-new").exists()


def test_prune_older_than_deletes_runs_past_the_ttl(tmp_path: Path, capsys) -> None:
    """Retention is by age as well as by count: a run older than the TTL goes even when
    it is among the newest `--keep`."""
    from datetime import UTC, datetime, timedelta

    runtime_dir = tmp_path / "runtime"
    now = datetime.now(UTC)
    old = make_state("run-old", (now - timedelta(days=60)).isoformat())
    new = make_state("run-new", now.isoformat())
    StateStore(runtime_dir / "runs" / "run-old" / "state.json").save(old)
    StateStore(runtime_dir / "runs" / "run-new" / "state.json").save(new)
    _prune_runs(runtime_dir, keep=10, dry_run=False, older_than_days=30)
    output = capsys.readouterr().out
    assert "pruned run-old" in output
    assert not (runtime_dir / "runs" / "run-old").exists()
    assert (runtime_dir / "runs" / "run-new").exists(), "a recent run survives the TTL"


def test_prune_rejects_a_non_positive_older_than(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="positive"):
        _prune_runs(tmp_path / "runtime", keep=10, dry_run=False, older_than_days=0)


def test_prune_uses_config_retention_when_the_flag_is_absent(
    tmp_path: Path, capsys
) -> None:
    """`[runtime] run_retention_days` applies unless --older-than overrides it."""
    from datetime import UTC, datetime, timedelta

    from gcae.cli import main

    runtime_dir = tmp_path / "runtime"
    now = datetime.now(UTC)
    StateStore(runtime_dir / "runs" / "run-old" / "state.json").save(
        make_state("run-old", (now - timedelta(days=60)).isoformat())
    )
    StateStore(runtime_dir / "runs" / "run-new" / "state.json").save(
        make_state("run-new", now.isoformat())
    )
    config = tmp_path / "config.toml"
    config.write_text("[runtime]\nrun_retention_days = 30\n")
    main(["prune", "--runtime-dir", str(runtime_dir),
          "--config", str(config), "--keep", "10"])
    output = capsys.readouterr().out
    assert "pruned run-old" in output
    assert (runtime_dir / "runs" / "run-new").exists()
