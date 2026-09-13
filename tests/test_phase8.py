from pathlib import Path

import pytest

from gcae.config import Config, EvaluatorConfig, ProviderConfig, load_config


def test_config_loading(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[runtime]\nmax_steps = 3\n[provider]\nkind = "http"\n')
    config = load_config(path)
    assert config.runtime.max_steps == 3
    assert config.provider.kind == "http"


def test_config_uses_xdg_state_home(monkeypatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/gcae-state")
    assert Config().state_dir == Path("/tmp/gcae-state/gcae")


def test_cli_reports_missing_config(capsys) -> None:
    from gcae.cli import main

    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                "/tmp/does-not-exist",
                "request",
                "--config",
                "/tmp/definitely-missing-gcae-config.toml",
            ]
        )
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "config file not found" in captured.err
    assert "Traceback" not in captured.err


def test_provider_kind_resolution() -> None:
    from gcae.cli import _provider
    from gcae.http_provider import OpenAICompatibleProvider
    from gcae.providers import FakeProvider

    assert isinstance(_provider(ProviderConfig(kind="fake")), FakeProvider)
    live = _provider(ProviderConfig(kind="openrouter"))
    assert isinstance(live, OpenAICompatibleProvider)
    live.close()
    with pytest.raises(ValueError):
        _provider(ProviderConfig(kind="bogus"))


def test_evaluator_kind_resolution() -> None:
    from gcae.cli import _evaluator
    from gcae.evaluator import DeterministicEvaluator, LLMEvaluator
    from gcae.providers import FakeProvider

    provider = FakeProvider([])
    assert isinstance(_evaluator(Config(), provider), DeterministicEvaluator)
    assert isinstance(
        _evaluator(Config(evaluator=EvaluatorConfig(kind="llm")), provider),
        LLMEvaluator,
    )
    with pytest.raises(ValueError):
        _evaluator(Config(evaluator=EvaluatorConfig(kind="bogus")), provider)


def test_cli_merge_asks_and_undo_reverses(tmp_path, capsys, monkeypatch) -> None:
    import subprocess
    import sys

    from gcae.cli import _maybe_merge, _undo
    from gcae.git import GitRepository
    from gcae.models import AgentState
    from gcae.persistence import StateStore

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "T"], check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)

    runtime_dir = tmp_path / "runtime"
    repo = GitRepository(source, runtime_dir)
    worktree, branch, base = repo.create_isolated_worktree("run-merge")
    (worktree / "feature.txt").write_text("feature\n")
    accepted = repo.checkpoint("feature")
    state = AgentState(
        run_id="run-merge",
        source_repo=str(source),
        worktree=str(worktree),
        branch=branch,
        objective="o",
        original_request="o",
        accepted_commit=accepted,
    )
    StateStore(runtime_dir / "runs" / "run-merge" / "state.json").save(state)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _maybe_merge(state, runtime_dir, merge_flag=False, no_merge_flag=False)
    assert "ready" in capsys.readouterr().err
    assert not (source / "feature.txt").exists()

    _maybe_merge(state, runtime_dir, merge_flag=True, no_merge_flag=False)
    assert (source / "feature.txt").exists()
    assert state.merge is not None
    capsys.readouterr()

    _undo(source, "run-merge", runtime_dir)
    assert not (source / "feature.txt").exists()
    assert repo.source_commit() == base
    reloaded = StateStore(runtime_dir / "runs" / "run-merge" / "state.json").load()
    assert reloaded.merge is None


def test_cli_merge_command_is_guarded_and_reversible(tmp_path) -> None:
    import subprocess

    from gcae.cli import _merge_run, _undo
    from gcae.git import GitRepository
    from gcae.models import AgentState
    from gcae.persistence import StateStore

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.email", "t@e.f"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "user.name", "T"], check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)

    runtime_dir = tmp_path / "runtime"
    repo = GitRepository(source, runtime_dir)
    worktree, branch, base = repo.create_isolated_worktree("run-merge")
    (worktree / "feature.txt").write_text("feature\n")
    accepted = repo.checkpoint("feature")
    state = AgentState(
        run_id="run-merge",
        source_repo=str(source),
        worktree=str(worktree),
        branch=branch,
        objective="o",
        original_request="o",
        status="running",
        accepted_commit=accepted,
    )
    state_path = runtime_dir / "runs" / "run-merge" / "state.json"
    StateStore(state_path).save(state)

    with pytest.raises(RuntimeError):
        _merge_run(source, "run-merge", runtime_dir)

    state.status = "complete"
    StateStore(state_path).save(state)
    (worktree / "drift.txt").write_text("drift\n")
    repo.checkpoint("drift")
    with pytest.raises(RuntimeError):
        _merge_run(source, "run-merge", runtime_dir)
    repo.rollback(accepted)

    _merge_run(source, "run-merge", runtime_dir)
    assert (source / "feature.txt").read_text() == "feature\n"
    assert StateStore(state_path).load().merge is not None
    with pytest.raises(RuntimeError):
        _merge_run(source, "run-merge", runtime_dir)

    _undo(source, "run-merge", runtime_dir)
    assert not (source / "feature.txt").exists()
    assert repo.source_commit() == base


def test_resume_restores_trusted_state(tmp_path: Path) -> None:
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime

    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
    (source / "a").write_text("a")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
    runtime = Runtime(source, tmp_path / "runtime", provider=FakeProvider([]))
    state = runtime.start("request")
    resumed = Runtime(source, tmp_path / "runtime", provider=FakeProvider([])).resume(state.run_id)
    assert resumed.accepted_commit == state.accepted_commit
    assert resumed.worktree == state.worktree


def _fake_config(tmp_path: Path) -> Path:
    config = tmp_path / "config.toml"
    config.write_text(
        f'[provider]\nkind = "fake"\n\n[runtime]\nstate_dir = "{tmp_path / "state"}"\n'
    )
    return config


def _git_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=T", "-c", "user.email=t@e.f",
            "commit", "-qm", "base",
        ],
        check=True,
    )
    return repo


def test_cli_exits_non_zero_when_a_run_does_not_complete(tmp_path: Path, capsys) -> None:
    """Scripts must be able to tell a failed run from a finished one."""
    from gcae.cli import main

    repo = _git_repo(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "run",
                str(repo),
                "create the impossible file",
                "--criterion",
                "file exists: never-created.txt",
                "--headless",
                "--no-merge",
                "--config",
                str(_fake_config(tmp_path)),
            ]
        )
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert "failed" in captured.err


def test_criterion_failure_evidence_shows_expected_and_found(tmp_path: Path) -> None:
    from gcae.models import AgentState
    from gcae.verifier import FinalVerifier

    def verify() -> object:
        state = AgentState(
            run_id="r",
            source_repo="/source",
            worktree=str(tmp_path),
            branch="b",
            objective="o",
            original_request="o",
            success_criteria=["file contains exactly: NOTES.md :: hello from gcae"],
        )
        return FinalVerifier().verify(state)

    # a conventional trailing newline is not a content difference
    (tmp_path / "NOTES.md").write_text("hello from gcae\n")
    assert verify().criteria[0].passed is True  # type: ignore[attr-defined]

    # extra content is, and the evidence shows both sides
    (tmp_path / "NOTES.md").write_text("hello from gcae\nand more\n")
    result = verify().criteria[0]  # type: ignore[attr-defined]
    assert result.passed is False
    assert "expected 'hello from gcae'" in result.evidence
    assert "and more" in result.evidence
