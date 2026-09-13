import subprocess
from pathlib import Path

import pytest

from gcae.git import GitError, GitRepository


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "main.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "base"], check=True)


def test_worktree_checkpoint_and_rollback(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, base = repo.create_isolated_worktree("run")
    (worktree / "candidate.txt").write_text("bad\n")
    assert (source / "candidate.txt").exists() is False
    repo.rollback(base)
    assert not (worktree / "candidate.txt").exists()
    (worktree / "good.txt").write_text("good\n")
    accepted = repo.checkpoint("accept good")
    assert accepted != base
    repo.remove_worktree()
    assert not worktree.exists()


def test_dirty_source_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / "main.txt").write_text("dirty\n")
    with pytest.raises(GitError):
        GitRepository(source, tmp_path / "runtime").create_isolated_worktree("run")


def test_rollback_removes_ignored_candidate_and_external_runtime_required(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / ".gitignore").write_text("*.cache\n")
    subprocess.run(["git", "-C", str(source), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "ignore"], check=True)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, base = repo.create_isolated_worktree("ignored")
    (worktree / "candidate.cache").write_text("bad")
    repo.rollback(base)
    assert not (worktree / "candidate.cache").exists()


def test_generated_artifacts_are_cleaned_only_in_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("generated")
    cache = worktree / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-313.pyc").write_bytes(b"cache")
    repo.clean_generated_artifacts()
    assert not cache.exists()
    repo.remove_worktree()
