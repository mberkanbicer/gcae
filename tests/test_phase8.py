from pathlib import Path

import pytest

from gcae.config import Config, EvaluatorConfig, ProviderConfig, load_config
from gcae.git import MergeConflict


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
        status="complete",
    )
    StateStore(runtime_dir / "runs" / "run-merge" / "state.json").save(state)

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    _maybe_merge(state, runtime_dir, merge_flag=False, no_merge_flag=False, auto_merge=False)
    assert "ready" in capsys.readouterr().err
    assert not (source / "feature.txt").exists()

    _maybe_merge(state, runtime_dir, merge_flag=True, no_merge_flag=False, auto_merge=False)
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


def _completed_run(tmp_path: Path):
    """A completed run that actually changed a file, built with the fake provider."""
    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    source = _git_repo(tmp_path)
    runtime = Runtime(
        source,
        tmp_path / "state",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "answer.txt", "content": "ok\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "create",
                    "reason_summary": "done",
                },
            ]
        ),
        control=RuntimeControl(),
    )
    runtime.start("create answer", success_criteria=["file exists: answer.txt"])
    assert runtime.run().status == "complete"
    return runtime


def test_auto_merge_puts_the_work_in_the_users_checkout(tmp_path: Path, capsys) -> None:
    from gcae.cli import _maybe_merge

    runtime = _completed_run(tmp_path)
    assert runtime.state is not None
    assert not (tmp_path / "repo" / "answer.txt").exists()
    _maybe_merge(runtime.state, tmp_path / "state", False, False, auto_merge=True)
    assert (tmp_path / "repo" / "answer.txt").read_text() == "ok\n"
    assert runtime.state.merge is not None
    assert "merged" in capsys.readouterr().err


def test_no_merge_flag_wins_over_auto_merge(tmp_path: Path, capsys) -> None:
    from gcae.cli import _maybe_merge

    runtime = _completed_run(tmp_path)
    assert runtime.state is not None
    _maybe_merge(runtime.state, tmp_path / "state", False, True, auto_merge=True)
    assert not (tmp_path / "repo" / "answer.txt").exists()
    assert runtime.state.merge is None


def test_auto_merge_off_keeps_the_branch_separate(tmp_path: Path, capsys) -> None:
    from gcae.cli import _maybe_merge

    runtime = _completed_run(tmp_path)
    assert runtime.state is not None
    _maybe_merge(runtime.state, tmp_path / "state", False, False, auto_merge=False)
    assert runtime.state.merge is None
    errors = capsys.readouterr().err
    assert "automatic merging is off" in errors
    assert "gcae merge" in errors  # the single command that does it, not raw git


def test_merge_command_reports_nothing_to_merge(tmp_path: Path, capsys) -> None:
    from gcae.cli import main

    repo = _git_repo(tmp_path)
    main(
        [
            "run",
            str(repo),
            "do nothing",
            "--criterion",
            "file exists: README.md",
            "--headless",
            "--no-merge",
            "--config",
            str(_fake_config(tmp_path)),
        ]
    )
    runs = sorted((tmp_path / "state" / "runs").glob("*/state.json"))
    run_id = runs[-1].parent.name
    main(["merge", str(repo), run_id, "--config", str(_fake_config(tmp_path))])
    assert "nothing to merge" in capsys.readouterr().err


def _failed_run_with_accepted_work(tmp_path: Path):
    """A run that accepted a checkpoint and then died (the slow-model case)."""
    from gcae.models import Evaluation
    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    class RejectThenStarve:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, payload):  # type: ignore[no-untyped-def]
            self.calls += 1
            if self.calls == 1:
                return Evaluation(decision="accept", reason="first step is good")
            return Evaluation(decision="rollback", reason="not converged")

    source = _git_repo(tmp_path)
    runtime = Runtime(
        source,
        tmp_path / "state",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "create",
                    "reason_summary": "create it",
                    "tool": {
                        "name": "create_file",
                        "arguments": {"path": "accepted.txt", "content": "kept\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "create",
                    "reason_summary": "done",
                },
            ]
        ),
        evaluator=RejectThenStarve(),
        control=RuntimeControl(),
        max_steps=2,
    )
    # an impossible criterion is what makes this run fail: a finished plan that satisfies its
    # criteria is verified and completes, so the failure has to be real, not a budget artefact
    runtime.start(
        "create the accepted file",
        success_criteria=["file exists: accepted.txt", "file exists: never-created.txt"],
    )
    result = runtime.run()
    assert result.status.startswith("failed"), result.status
    assert result.accepted_steps == 1
    assert result.accepted_commit is not None
    return runtime


def test_manual_merge_rescues_accepted_work_from_a_failed_run(tmp_path: Path, capsys) -> None:
    from gcae.cli import _merge_run

    runtime = _failed_run_with_accepted_work(tmp_path)
    assert runtime.state is not None
    assert not (tmp_path / "repo" / "accepted.txt").exists()
    _merge_run(tmp_path / "repo", runtime.state.run_id, tmp_path / "state")
    errors = capsys.readouterr().err
    assert "without final verification" in errors
    assert (tmp_path / "repo" / "accepted.txt").read_text() == "kept\n"
    from gcae.persistence import StateStore

    persisted = StateStore(
        tmp_path / "state" / "runs" / runtime.state.run_id / "state.json"
    ).load()
    assert persisted.merge is not None


def test_failed_run_without_accepted_work_is_not_mergeable(tmp_path: Path, capsys) -> None:
    from gcae.cli import _merge_run

    repo = _git_repo(tmp_path)
    main = __import__("gcae.cli", fromlist=["main"]).main
    with pytest.raises(SystemExit):
        main(
            [
                "run",
                str(repo),
                "do something impossible",
                "--criterion",
                "file exists: missing.txt",
                "--headless",
                "--no-merge",
                "--config",
                str(_fake_config(tmp_path)),
            ]
        )
    runs = sorted((tmp_path / "state" / "runs").glob("*/state.json"))
    run_id = runs[-1].parent.name
    with pytest.raises(RuntimeError, match="not complete"):
        _merge_run(repo, run_id, tmp_path / "state")
    # no misleading "merging 0 accepted step(s)" warning
    assert "0 accepted step" not in capsys.readouterr().err


def test_summary_reports_where_the_documents_are(tmp_path: Path, capsys) -> None:
    """The user must be able to find the produced files without guessing."""
    from gcae.cli import _summary
    from gcae.git import GitRepository

    runtime = _completed_run(tmp_path)
    assert runtime.state is not None
    assert runtime.repo is not None
    repo = GitRepository(tmp_path / "repo", tmp_path / "state")
    files = repo.files_between(repo.current_branch(), runtime.state.branch)
    assert files == ["answer.txt"]

    summary = _summary(runtime.state, files)
    assert "files: answer.txt" in summary
    assert "worktree on branch" in summary
    assert "nothing is in your checkout until GCAE merges it" in summary
    assert str(runtime.state.worktree) in summary

    runtime.merge_completed_run()
    # after merging, the file list must still be computed (against the pre-merge commit)
    from gcae.cli import _run_files

    files_after_merge = _run_files(runtime.state, runtime.repo)
    assert files_after_merge == ["answer.txt"]
    merged_summary = _summary(runtime.state, files_after_merge)
    assert f"documents: {tmp_path / 'repo'} (in your working tree now)" in merged_summary
    assert (tmp_path / "repo" / "answer.txt").exists()


def test_cli_handles_every_git_step_without_user_action(tmp_path: Path, capsys) -> None:
    """The name says Git-Checkpointed: the loop, not the user, does the git work.

    One scenario with every precondition at once: unborn repository, no identity, dirty
    working tree, a run that fails after accepting work, and a worktree the user deleted.
    """
    import subprocess

    from gcae.cli import _maybe_merge, _run_files, _summary
    from gcae.git import GitRepository
    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    repo = tmp_path / "fresh"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "notes.txt").write_text("my work in progress\n")

    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    import os

    previous = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        runtime = Runtime(
            repo,
            tmp_path / "state",
            provider=FakeProvider(
                [
                    {
                        "action": "execute_tool",
                        "semantic_goal": "a",
                        "reason_summary": "a",
                        "tool": {
                            "name": "create_file",
                            "arguments": {"path": "docs/a.md", "content": "# A\n"},
                        },
                    },
                    {
                        "action": "complete_semantic_step",
                        "semantic_goal": "a",
                        "reason_summary": "done",
                    },
                ]
            ),
            control=RuntimeControl(),
            max_steps=2,
            cleanup_after_merge=True,
        )
        runtime.start("write docs", success_criteria=["file exists: nope.md"])
        state = runtime.run()
        assert state.status.startswith("failed")  # criterion never satisfiable
        assert state.accepted_steps == 1
        _maybe_merge(state, tmp_path / "state", False, False, auto_merge=True)
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    repo_handle = GitRepository(repo, tmp_path / "state")
    # 1. accepted work reached the checkout even though the run failed
    assert (repo / "docs" / "a.md").read_text() == "# A\n"
    # 2. the unborn repository got a base commit, with the user's WIP committed, not lost
    assert (repo / "notes.txt").exists()
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "base commit" in log and "gcae:" in log
    # 3. commits were authored without any configured git identity
    author = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%ae"], capture_output=True, text=True
    ).stdout.strip()
    assert author == "gcae@localhost"
    # 4. GCAE cleaned up its own worktree; the branch survives for undo/re-merge
    assert not Path(state.worktree).exists()
    branches = subprocess.run(
        ["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert state.branch in branches
    record = state.merge
    assert record is not None
    repo_handle.undo_merge(record.pre_merge_commit, record.merge_commit)
    assert not (repo / "docs" / "a.md").exists()          # undo still works
    repo_handle.merge_branch(state.branch)                 # and re-merging is possible
    assert (repo / "docs" / "a.md").exists()
    # 5. nothing in the summary tells the user to run git
    summary = _summary(state, _run_files(state, repo_handle))
    assert "git merge" not in summary
    assert "documents: " in summary and str(repo) in summary


def test_no_change_run_cleans_up_and_says_so(tmp_path: Path, capsys) -> None:
    """An empty run must leave nothing behind and must not ask for a merge."""
    from gcae.cli import main

    repo = _git_repo(tmp_path)
    config = _fake_config(tmp_path)
    main(
        [
            "run",
            str(repo),
            "do nothing",
            "--criterion",
            "file exists: README.md",
            "--headless",
            "--config",
            str(config),
        ]
    )
    errors = capsys.readouterr().err
    assert "nothing to merge" in errors
    assert "removed the worktree of the empty run" in errors
    worktrees = tmp_path / "state" / "worktrees"
    assert not any(worktrees.iterdir()) if worktrees.exists() else True
    # and the summary no longer suggests a merge either
    from gcae.persistence import StateStore

    runs = sorted((tmp_path / "state" / "runs").glob("*/state.json"))
    state = StateStore(runs[-1]).load()
    from gcae.cli import _summary

    summary = _summary(state, [])
    assert "no file changes; nothing to merge" in summary
    assert "documents: none — the run produced no files" in summary


def _conflicting_repo(tmp_path: Path) -> tuple[Path, str, str]:
    """A repo where the run branch and the checked-out branch edit the same line."""
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "app.py").write_text("value = 1\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-qm", "base"],
        check=True,
    )

    runtime = Runtime(
        repo,
        tmp_path / "state",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "change",
                    "reason_summary": "set the value",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "app.py", "content": "value = 2\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "change",
                    "reason_summary": "done",
                },
            ]
        ),
        control=RuntimeControl(),
        auto_merge=False,
    )
    runtime.start("set the value", success_criteria=["command succeeds: test -f app.py"])
    state = runtime.run()
    assert state.status == "complete"

    # the user's branch moves on the same line -> a real conflict
    (repo / "app.py").write_text("value = 99\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@e.f",
         "commit", "-qm", "user edit"],
        check=True,
    )
    return repo, state.run_id, runtime.state.worktree


def test_conflicting_merge_is_resolved_by_the_agent(tmp_path: Path) -> None:
    """The only git process that needed judgement now goes through the loop."""
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    repo, run_id, worktree = _conflicting_repo(tmp_path)

    # the merge really conflicts, and the user's checkout is left untouched and clean
    runtime = Runtime(repo, tmp_path / "state", provider=FakeProvider([]), control=RuntimeControl())
    runtime.resume(run_id)
    with pytest.raises(MergeConflict) as conflict:
        runtime.merge_completed_run()
    assert conflict.value.files == ["app.py"]
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip() == ""
    assert (repo / "app.py").read_text() == "value = 99\n"  # nothing of the run landed yet

    # the agent resolves the conflict inside its own worktree and re-verifies
    resolver = Runtime(
        repo,
        tmp_path / "state",
        provider=FakeProvider(
            [
                {
                    "action": "execute_tool",
                    "semantic_goal": "resolve",
                    "reason_summary": "keep the run's value",
                    "tool": {
                        "name": "write_file",
                        "arguments": {"path": "app.py", "content": "value = 2\n"},
                    },
                },
                {
                    "action": "complete_semantic_step",
                    "semantic_goal": "resolve",
                    "reason_summary": "resolved",
                },
            ]
        ),
        control=RuntimeControl(),
        auto_merge=False,
    )
    resolver.resume(run_id)
    assert resolver.resolve_merge_conflicts() == ["app.py"]
    assert resolver.state is not None and resolver.state.status == "complete"
    assert Path(worktree).exists() and not resolver.repo.worktree_merge_in_progress()

    # now the merge is a fast-forward and the resolved content reaches the checkout
    resolver.merge_completed_run()
    assert (repo / "app.py").read_text() == "value = 2\n"
    log = subprocess.run(
        ["git", "-C", str(repo), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert "gcae:" in log


def test_unresolved_conflict_leaves_the_branch_and_checkout_intact(tmp_path: Path) -> None:
    """An agent that fails to resolve must not deliver conflict markers."""
    import subprocess

    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    repo, run_id, worktree = _conflicting_repo(tmp_path)
    resolver = Runtime(
        repo,
        tmp_path / "state",
        provider=FakeProvider(
            [
                {
                    "action": "finish_candidate",
                    "semantic_goal": "give up",
                    "reason_summary": "cannot resolve",
                }
            ]
        ),
        control=RuntimeControl(),
        auto_merge=False,
        max_steps=3,
    )
    resolver.resume(run_id)
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    assert resolver.resolve_merge_conflicts() == []
    # no merge commit was created, no markers committed, no merge left in progress
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip() == before
    assert (repo / "app.py").read_text() == "value = 99\n"
    assert "&lt;&lt;&lt;&lt;&lt;&lt;&lt;" not in (repo / "app.py").read_text()
    assert not resolver.repo.worktree_merge_in_progress()
    assert subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True
    ).stdout.strip() == ""


def test_merge_command_hands_conflicts_to_the_agent(tmp_path: Path, capsys) -> None:
    """`gcae merge` must not bounce a conflict back to the user either."""
    import subprocess

    from gcae.cli import main
    from gcae.providers import FakeProvider
    from gcae.runtime import Runtime, RuntimeControl

    repo, run_id, worktree = _conflicting_repo(tmp_path)
    config = _fake_config(tmp_path)  # the fake provider cannot resolve -> honest failure
    before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()

    with pytest.raises(SystemExit) as exit_info:
        main(["merge", str(repo), run_id, "--config", str(config)])
    assert exit_info.value.code == 1
    errors = capsys.readouterr().err
    assert "merge conflicts in app.py" in errors
    assert "handing them to the agent" in errors
    assert "conflicts remain" in errors
    # nothing of the conflicting work reached the checkout, and no markers were committed
    assert subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip() == before
    assert (repo / "app.py").read_text() == "value = 99\n"
    assert "&lt;&lt;&lt;&lt;&lt;&lt;&lt;" not in (repo / "app.py").read_text()
    # a later manual merge still works once the conflict is gone
    (repo / "app.py").write_text("value = 99\n")
    merged = Runtime(repo, tmp_path / "state", provider=FakeProvider([]), control=RuntimeControl())
    merged.resume(run_id)
    merged.repo.rollback(merged.state.accepted_commit)  # type: ignore[arg-type]


def test_completed_run_whose_merge_conflicts_exits_non_zero(tmp_path: Path, capsys) -> None:
    """A finished run whose work never reached the checkout is not a success."""
    from gcae.cli import main

    repo, run_id, worktree = _conflicting_repo(tmp_path)
    config = _fake_config(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        main(["resume", str(repo), run_id, "--headless", "--config", str(config)])
    assert exit_info.value.code == 1
    errors = capsys.readouterr().err
    assert "conflicts remain" in errors
    persisted = (tmp_path / "state" / "runs" / run_id / "state.json").read_text()
    assert '"merge": {' not in persisted
