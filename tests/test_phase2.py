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


def test_dirty_source_is_committed_automatically(tmp_path: Path) -> None:
    """A run needs a defined base: GCAE commits the working tree instead of refusing."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / "main.txt").write_text("base\ndirty\n")
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, base = repo.create_isolated_worktree("run")
    assert (worktree / "main.txt").read_text() == "base\ndirty\n"
    assert repo.notices and repo.notices[0]["kind"] == "base"
    assert repo.notices[0]["commit"] == base
    assert repo.notices[0]["files"] == 1
    # file contents are untouched and the source tree is clean afterwards
    assert (source / "main.txt").read_text() == "base\ndirty\n"
    assert repo._run("status", "--porcelain") == ""


def test_dirty_source_rejected_when_bootstrap_disabled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / "main.txt").write_text("dirty\n")
    repo = GitRepository(source, tmp_path / "runtime", auto_bootstrap=False)
    with pytest.raises(GitError, match="main.txt"):
        repo.create_isolated_worktree("run")


def test_source_subdirectory_is_refused_with_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    nested = source / "pkg"
    nested.mkdir()
    with pytest.raises(GitError, match="repository root"):
        GitRepository(nested, tmp_path / "runtime").create_isolated_worktree("run")


def test_missing_git_identity_falls_back_and_is_reported(tmp_path: Path, monkeypatch) -> None:
    """Commits must work on a fresh machine; the fallback identity is reported, not silent."""
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    subprocess.run(["git", "-C", str(source), "config", "--unset", "user.email"], check=True)
    subprocess.run(["git", "-C", str(source), "config", "--unset", "user.name"], check=True)
    (source / "main.txt").write_text("base\nchanged\n")
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, base = repo.create_isolated_worktree("run")
    kinds = [notice["kind"] for notice in repo.notices]
    assert "identity" in kinds and "base" in kinds
    author = repo._run("log", "-1", "--format=%an <%ae>", cwd=source)
    assert author == "GCAE <gcae@localhost>"
    (worktree / "checkpoint.txt").write_text("x\n")
    repo.checkpoint("gcae: step")
    assert repo._run("log", "-1", "--format=%ae", cwd=worktree) == "gcae@localhost"
    assert base


def test_modified_file_path_is_parsed_correctly(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, _ = repo.create_isolated_worktree("paths")
    (worktree / "main.txt").write_text("changed\n")
    assert repo.changed_files() == ["main.txt"]
    assert repo.status_entries() == [(" M", "main.txt")]


def test_repository_without_commits_is_bootstrapped(tmp_path: Path) -> None:
    """`git init` followed by a GCAE run must work with no manual Git step."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    (source / "main.txt").write_text("hello\n")
    (source / ".gitignore").write_text("ignored.txt\n")
    (source / "ignored.txt").write_text("must stay untracked\n")
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, _, base = repo.create_isolated_worktree("run")
    assert (worktree / "main.txt").read_text() == "hello\n"
    assert not (worktree / "ignored.txt").exists()
    assert repo.notices[0]["kind"] == "base"
    assert repo.notices[0]["files"] == 2  # main.txt and .gitignore, not the ignored file
    assert base == repo.notices[0]["commit"]
    assert repo._run("rev-parse", "--abbrev-ref", "HEAD") == "main"


def test_empty_repository_gets_an_empty_base_commit(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    repo = GitRepository(source, tmp_path / "runtime")
    worktree, branch, base = repo.create_isolated_worktree("run")
    assert worktree.exists() and branch == "gcae/run"
    assert repo.notices[0]["kind"] == "empty"
    assert repo.notices[0]["files"] == 0
    assert base == repo.notices[0]["commit"]


def test_repository_without_commits_rejected_when_bootstrap_disabled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    repo = GitRepository(source, tmp_path / "runtime", auto_bootstrap=False)
    with pytest.raises(GitError, match="no commits"):
        repo.create_isolated_worktree("run")


def test_bootstrap_refuses_an_unbounded_working_tree(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    for index in range(3):
        (source / f"file{index}.txt").write_text("x\n")
    monkeypatch.setattr("gcae.git.MAX_BOOTSTRAP_FILES", 2)
    repo = GitRepository(source, tmp_path / "runtime")
    with pytest.raises(GitError, match="will not auto-commit"):
        repo.create_isolated_worktree("run")
    assert len(repo.source_status_entries()) == 3  # nothing was staged


def test_bootstrap_refuses_an_in_progress_merge(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    init_repo(source)
    (source / ".git" / "MERGE_HEAD").write_text("0" * 40 + "\n")
    (source / "main.txt").write_text("conflicted\n")
    repo = GitRepository(source, tmp_path / "runtime")
    with pytest.raises(GitError, match="in-progress merge"):
        repo.create_isolated_worktree("run")


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
