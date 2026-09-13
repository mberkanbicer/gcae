from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

# Auto-bootstrap safety bounds: GCAE creates the base commit a run needs, but never
# swallows an unbounded amount of the user's working tree in the process.
MAX_BOOTSTRAP_FILES = 2_000
MAX_BOOTSTRAP_BYTES = 50 * 1024 * 1024
FALLBACK_GIT_NAME = "GCAE"
FALLBACK_GIT_EMAIL = "gcae@localhost"


class GitError(RuntimeError):
    """Raised when a Git safety invariant or command fails."""


class GitRepository:
    def __init__(
        self,
        source: str | Path,
        runtime_dir: str | Path,
        worktree_dir: str | Path | None = None,
        auto_bootstrap: bool = True,
    ) -> None:
        self.source = Path(source).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.worktree_dir = (
            Path(worktree_dir).resolve()
            if worktree_dir is not None
            else self.runtime_dir / "worktrees"
        )
        self.auto_bootstrap = auto_bootstrap
        self.worktree: Path | None = None
        self.branch: str | None = None
        self.notices: list[dict[str, Any]] = []
        self._identity_noticed = False

    def _run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=cwd or self.source,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitError(
                "git executable not found; install Git and make sure it is on PATH"
            ) from exc
        if check and result.returncode != 0:
            raise GitError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout.rstrip("\n")

    def validate_source(self) -> str:
        """Check every precondition, repairing the ones GCAE can repair safely.

        A run needs a committed base inside its isolated worktree. When the source
        repository has no commits, or its working tree is dirty, GCAE creates that base
        commit itself (bounded, ``.gitignore`` respected, reported through notices)
        instead of refusing to start. ``auto_bootstrap=False`` restores refusal.
        """
        if not self.source.is_dir():
            raise GitError(f"source is not a directory: {self.source}")
        if self._run("rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise GitError(f"source is not a Git repository: {self.source}")
        top = self._run("rev-parse", "--show-toplevel", check=False)
        if top and Path(top).resolve() != self.source:
            raise GitError(
                f"source is a subdirectory of a Git repository; pass the repository root: {top}"
            )
        for directory in (self.runtime_dir, self.worktree_dir):
            if directory == self.source or self.source in directory.parents:
                raise GitError("runtime directories must be external to the source repository")
        if self.auto_bootstrap:
            self.bootstrap_source_repository()
        if not self._run("rev-parse", "--verify", "HEAD", check=False):
            raise GitError(
                f"source repository has no commits: {self.source} — create a base commit "
                'first (git add -A; git commit -m "base") or drop --no-auto-bootstrap '
                "to let GCAE create it"
            )
        self._identity_args()
        status = self._run("status", "--porcelain", "--untracked-files=all")
        if status:
            lines = status.splitlines()
            paths = ", ".join(line[3:] for line in lines[:10])
            more = "" if len(lines) <= 10 else f" (+{len(lines) - 10} more)"
            raise GitError(
                "source repository has uncommitted changes; commit or stash them first: "
                f"{paths}{more}"
            )
        return self._run("rev-parse", "HEAD")

    # ------------------------------------------------------------------ bootstrap

    def _git_dir(self) -> Path:
        value = self._run("rev-parse", "--absolute-git-dir", check=False)
        return Path(value) if value else self.source / ".git"

    def _in_progress_operation(self) -> str | None:
        git_dir = self._git_dir()
        markers = (
            ("MERGE_HEAD", "merge"),
            ("CHERRY_PICK_HEAD", "cherry-pick"),
            ("REVERT_HEAD", "revert"),
            ("rebase-merge", "rebase"),
            ("rebase-apply", "rebase"),
        )
        for marker, label in markers:
            if (git_dir / marker).exists():
                return label
        return None

    def _identity_args(self) -> list[str]:
        """Commit identity: the user's own when configured, a neutral fallback otherwise."""
        if self._run("config", "user.email", check=False) and self._run(
            "config", "user.name", check=False
        ):
            return []
        if not self._identity_noticed:
            self._identity_noticed = True
            self.notices.append(
                {
                    "kind": "identity",
                    "message": (
                        "git user.name/user.email are not configured; commits use "
                        f"{FALLBACK_GIT_NAME} <{FALLBACK_GIT_EMAIL}>"
                    ),
                }
            )
        return [
            "-c",
            f"user.name={FALLBACK_GIT_NAME}",
            "-c",
            f"user.email={FALLBACK_GIT_EMAIL}",
        ]

    def _commit(self, message: str, allow_empty: bool = False) -> str:
        args = [*self._identity_args(), "commit", "-m", message]
        if allow_empty:
            args.append("--allow-empty")
        self._run(*args)
        return self._run("rev-parse", "HEAD")

    def source_status_entries(self) -> list[tuple[str, str]]:
        output = self._run("status", "--porcelain", "--untracked-files=all")
        return [(line[:2], line[3:]) for line in output.splitlines() if len(line) >= 4]

    def _entry_size(self, path: str) -> int:
        try:
            target = self.source / path
            if target.is_symlink():
                return len(str(target.readlink()))
            return target.stat().st_size if target.is_file() else 0
        except OSError:
            return 0

    def bootstrap_source_repository(self) -> dict[str, Any] | None:
        """Create the base commit a run needs. Returns a report or None when clean.

        Never touches file contents: it only adds and commits what is already there, so the
        working tree stays byte-identical and the commit can be undone with
        ``git reset --soft HEAD~1``. Untracked-but-ignored files stay untracked.
        """
        operation = self._in_progress_operation()
        if operation is not None:
            raise GitError(
                f"source repository has an in-progress {operation}; finish or abort it "
                "before running GCAE"
            )
        entries = self.source_status_entries()
        if not entries:
            if self._run("rev-parse", "--verify", "HEAD", check=False):
                return None
            commit = self._commit("gcae: base commit (empty repository)", allow_empty=True)
            report: dict[str, Any] = {
                "kind": "empty",
                "commit": commit,
                "files": 0,
                "bytes": 0,
            }
        else:
            paths = [path for _, path in entries]
            total = sum(self._entry_size(path) for path in paths)
            if len(paths) > MAX_BOOTSTRAP_FILES or total > MAX_BOOTSTRAP_BYTES:
                raise GitError(
                    f"source repository has {len(paths)} uncommitted files "
                    f"({total / 1_000_000:.1f} MB) — GCAE will not auto-commit that much. "
                    "Commit or stash them first, or add the intended files to .gitignore"
                )
            self._run("add", "-A")
            commit = self._commit("gcae: base commit of the current working tree")
            report = {
                "kind": "base",
                "commit": commit,
                "files": len(paths),
                "bytes": total,
            }
        self.notices.append(
            {
                **report,
                "message": (
                    f"created base commit {commit[:7]} from {report['files']} files"
                    if report["files"]
                    else f"created empty base commit {commit[:7]}"
                ),
            }
        )
        return report

    def create_isolated_worktree(self, run_id: str | None = None) -> tuple[Path, str, str]:
        base = self.validate_source()
        run_id = run_id or uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise GitError("run_id contains unsupported characters")
        branch = f"gcae/{run_id}"
        worktree = self.worktree_dir / run_id
        if worktree.exists():
            raise GitError(f"worktree path already exists: {worktree}")
        worktree.parent.mkdir(parents=True, exist_ok=True)
        self._run("worktree", "add", "-b", branch, str(worktree), base)
        self.worktree = worktree
        self.branch = branch
        return worktree, branch, base

    def _require_worktree(self) -> Path:
        if self.worktree is None:
            raise GitError("isolated worktree has not been created")
        return self.worktree

    def branch_head(self, branch: str) -> str:
        return self._run("rev-parse", f"refs/heads/{branch}")

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def source_commit(self) -> str:
        return self._run("rev-parse", "HEAD")

    def merge_branch(self, branch: str) -> tuple[str, str]:
        """Merge branch into the source repository; returns (pre_merge, merge) commits."""
        if self._run("status", "--porcelain", check=False):
            raise GitError("source repository has uncommitted changes; refusing to merge")
        pre = self.source_commit()
        try:
            self._run("merge", "--ff-only", branch)
        except GitError:
            try:
                self._run("merge", "--no-ff", "--no-edit", branch)
            except GitError as exc:
                self._run("merge", "--abort", check=False)
                raise GitError(f"cannot merge {branch}: {exc}") from exc
        merged = self.source_commit()
        if merged == pre:
            raise GitError(f"branch {branch} is already merged")
        return pre, merged

    def undo_merge(self, pre_merge_commit: str, merge_commit: str) -> None:
        """Reset the source branch to the recorded pre-merge commit."""
        if self._run("status", "--porcelain", check=False):
            raise GitError("source repository has uncommitted changes; refusing to undo merge")
        if self.source_commit() != merge_commit:
            raise GitError("source HEAD moved since the merge; refusing to undo")
        self._run("reset", "--hard", pre_merge_commit)

    def assert_registered_worktree(self) -> None:
        worktree = self._require_worktree().resolve()
        listing = self._run("worktree", "list", "--porcelain", cwd=self.source)
        registered = {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }
        if worktree not in registered:
            raise GitError(f"worktree is not registered with the source repository: {worktree}")

    def current_commit(self) -> str:
        return self._run("rev-parse", "HEAD", cwd=self._require_worktree())

    def status(self) -> str:
        return self._run("status", "--porcelain", cwd=self._require_worktree())

    def diff(self) -> str:
        return self._run("diff", "--no-ext-diff", cwd=self._require_worktree())

    def diff_stat(self) -> str:
        return self._run("diff", "--stat", cwd=self._require_worktree())

    def diff_check(self) -> bool:
        return self._run("diff", "--check", cwd=self._require_worktree(), check=False) == ""

    def clean_generated_artifacts(self) -> None:
        worktree = self._require_worktree()
        output = self._run(
            "status",
            "--porcelain",
            "--ignored",
            "--untracked-files=all",
            cwd=worktree,
        )
        generated_names = {
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            ".coverage",
        }
        for line in output.splitlines():
            if len(line) < 4 or line[:2] not in {"??", "!!"}:
                continue
            relative = line[3:].rstrip("/")
            path = (worktree / relative).resolve()
            if path != worktree and worktree not in path.parents:
                raise GitError("generated artifact path escaped worktree")
            if path.name in generated_names or path.suffix == ".pyc":
                if path.is_dir():
                    shutil.rmtree(path)
                elif path.exists():
                    path.unlink()
                    parent = path.parent
                    while (
                        parent != worktree
                        and parent.name in generated_names
                        and not any(parent.iterdir())
                    ):
                        parent.rmdir()
                        parent = parent.parent

    def clean_ignored_artifacts(self) -> None:
        self._run("clean", "-fdX", cwd=self._require_worktree())

    def changed_files(self) -> list[str]:
        return [path for _, path in self.status_entries()]

    def status_entries(self) -> list[tuple[str, str]]:
        """Porcelain status at file level (``-uall`` lists new files, not directories)."""
        output = self._run(
            "status", "--porcelain", "--untracked-files=all", cwd=self._require_worktree()
        )
        return [(line[:2], line[3:]) for line in output.splitlines() if len(line) >= 4]

    def numstat(self) -> dict[str, tuple[int | None, int | None]]:
        """Insertions/deletions per tracked file (None for binary files)."""
        output = self._run("diff", "--numstat", cwd=self._require_worktree())
        stats: dict[str, tuple[int | None, int | None]] = {}
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) != 3:
                continue
            added, deleted, path = parts
            stats[path] = (
                int(added) if added.isdigit() else None,
                int(deleted) if deleted.isdigit() else None,
            )
        return stats

    def count_lines(self, relative: str, limit: int = 1_000_000) -> int | None:
        """Line count for an untracked file; None for binary or oversized files."""
        path = self._require_worktree() / relative
        try:
            if not path.is_file() or path.stat().st_size > limit:
                return None
            data = path.read_bytes()
        except OSError:
            return None
        if b"\x00" in data[:8192]:
            return None
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)

    def candidate_snapshot(self) -> dict[str, Any]:
        """Structured candidate state for the UI: no decoration, no invented numbers."""
        stats = self.numstat()
        files: list[dict[str, Any]] = []
        added_total = 0
        deleted_total = 0
        for code, path in self.status_entries():
            added, deleted = stats.get(path, (None, None))
            if code == "??":
                added = self.count_lines(path)
                deleted = 0 if added is not None else None
            if added is not None:
                added_total += added
            if deleted is not None:
                deleted_total += deleted
            files.append({"code": code, "path": path, "added": added, "deleted": deleted})
        return {
            "dirty": bool(files),
            "files": files,
            "added": added_total,
            "deleted": deleted_total,
        }

    def diff_by_file(self, limit: int = 120_000) -> list[dict[str, Any]]:
        """Unified diff per changed file, including untracked files, bounded in size."""
        worktree = self._require_worktree()
        result: list[dict[str, Any]] = []
        for code, path in self.status_entries():
            if code == "??":
                text = self._run(
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--",
                    "/dev/null",
                    path,
                    cwd=worktree,
                    check=False,
                )
            else:
                text = self._run("diff", "--no-ext-diff", "--", path, cwd=worktree)
            truncated = len(text) > limit
            result.append(
                {
                    "code": code,
                    "path": path,
                    "diff": text[:limit],
                    "truncated": truncated,
                }
            )
        return result

    def head_subject(self, ref: str = "HEAD") -> str:
        return self._run("log", "-1", "--pretty=%s", ref, cwd=self._require_worktree())

    def checkpoint(self, message: str) -> str:
        worktree = self._require_worktree()
        self._run("add", "-A", cwd=worktree)
        if not self.status():
            raise GitError("cannot create checkpoint with no changes")
        args = [*self._identity_args(), "commit", "-m", message]
        self._run(*args, cwd=worktree)
        return self.current_commit()

    def rollback(self, accepted_commit: str) -> None:
        worktree = self._require_worktree()
        self._run("reset", "--hard", accepted_commit, cwd=worktree)
        self._run("clean", "-fdx", cwd=worktree)
        if self.current_commit() != accepted_commit or self.status():
            raise GitError("rollback failed to restore clean accepted checkpoint")

    def remove_worktree(self) -> None:
        if self.worktree is None:
            return
        self._run("worktree", "remove", "--force", str(self.worktree))
        self.worktree = None

    def branch_exists(self) -> bool:
        return self.branch is not None and bool(
            self._run("show-ref", "--verify", f"refs/heads/{self.branch}", check=False)
        )
