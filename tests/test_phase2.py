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


def test_candidate_snapshot_counts_real_changes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("snapshot")
    (worktree / "main.txt").write_text("base\nmodified\n")
    (worktree / "new.txt").write_text("one\ntwo\nthree\n")
    snapshot = repo.candidate_snapshot()
    assert snapshot["dirty"] is True
    assert snapshot["added"] == 4
    assert snapshot["deleted"] == 0
    by_path = {entry["path"]: entry for entry in snapshot["files"]}
    assert by_path["main.txt"]["code"] == " M"
    assert by_path["main.txt"]["added"] == 1
    assert by_path["new.txt"]["code"] == "??"
    assert by_path["new.txt"]["added"] == 3


def test_candidate_snapshot_of_clean_worktree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    repo.create_isolated_worktree("clean")
    snapshot = repo.candidate_snapshot()
    assert snapshot == {"dirty": False, "files": [], "added": 0, "deleted": 0}


def test_diff_by_file_includes_untracked_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("diff")
    (worktree / "main.txt").write_text("base\nmodified\n")
    (worktree / "fresh.txt").write_text("fresh line\n")
    files = {entry["path"]: entry for entry in repo.diff_by_file()}
    assert "+modified" in files["main.txt"]["diff"]
    assert "+fresh line" in files["fresh.txt"]["diff"]
    assert files["fresh.txt"]["code"] == "??"


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
    with pytest.raises(GitError, match="main.txt"):
        GitRepository(source, tmp_path / "runtime").create_isolated_worktree("run")


def test_source_subdirectory_is_refused_with_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    nested = source / "pkg"
    nested.mkdir()
    with pytest.raises(GitError, match="repository root"):
        GitRepository(nested, tmp_path / "runtime").create_isolated_worktree("run")


def test_missing_git_identity_is_refused(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    subprocess.run(
        ["git", "-C", str(source), "config", "--unset", "user.email"], check=True
    )
    with pytest.raises(GitError, match="user.email is not configured"):
        GitRepository(source, tmp_path / "runtime").create_isolated_worktree("run")


def test_modified_file_path_is_parsed_correctly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("paths")
    (worktree / "main.txt").write_text("changed\n")
    assert repo.changed_files() == ["main.txt"]
    assert repo.status_entries() == [(" M", "main.txt")]


def test_repository_without_commits_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    with pytest.raises(GitError, match="no commits"):
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


def test_merge_branch_is_reversible(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, branch, base = repo.create_isolated_worktree("merge")
    (worktree / "feature.txt").write_text("feature\n")
    repo.checkpoint("feature")
    repo.remove_worktree()
    pre, merged = repo.merge_branch(branch)
    assert pre == base
    assert merged != base
    assert (source / "feature.txt").read_text() == "feature\n"
    repo.undo_merge(pre, merged)
    assert not (source / "feature.txt").exists()
    assert repo.source_commit() == base


def test_merge_and_undo_guards(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, branch, _ = repo.create_isolated_worktree("merge")
    (worktree / "feature.txt").write_text("feature\n")
    repo.checkpoint("feature")
    repo.remove_worktree()
    (source / "dirty.txt").write_text("dirty\n")
    with pytest.raises(GitError):
        repo.merge_branch(branch)
    (source / "dirty.txt").unlink()
    pre, merged = repo.merge_branch(branch)
    (source / "later.txt").write_text("later\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "later"], check=True)
    with pytest.raises(GitError):
        repo.undo_merge(pre, merged)


def test_untracked_directories_are_reported_at_file_level(tmp_path: Path) -> None:
    """`?? tests/` would break scope checks, line counts and the diff view."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("untracked")
    (worktree / "newdir").mkdir()
    (worktree / "newdir" / "one.txt").write_text("a\nb\n")
    (worktree / "newdir" / "two.txt").write_text("c\n")
    assert repo.status_entries() == [("??", "newdir/one.txt"), ("??", "newdir/two.txt")]
    assert repo.changed_files() == ["newdir/one.txt", "newdir/two.txt"]
    snapshot = repo.candidate_snapshot()
    assert snapshot["added"] == 3
    assert {entry["path"] for entry in snapshot["files"]} == {"newdir/one.txt", "newdir/two.txt"}
    files = {entry["path"]: entry for entry in repo.diff_by_file()}
    assert "+a" in files["newdir/one.txt"]["diff"]
