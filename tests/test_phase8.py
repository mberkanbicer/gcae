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

    assert isinstance(_provider(Config(provider=ProviderConfig(kind="fake"))), FakeProvider)
    live = _provider(Config(provider=ProviderConfig(kind="openrouter")))
    assert isinstance(live, OpenAICompatibleProvider)
    live.close()
    with pytest.raises(ValueError):
        _provider(Config(provider=ProviderConfig(kind="bogus")))


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
