from pathlib import Path

from gcae.config import load_config


def test_config_loading(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text('[runtime]\nmax_steps = 3\n[provider]\nkind = "http"\n')
    config = load_config(path)
    assert config.runtime.max_steps == 3
    assert config.provider.kind == "http"


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
