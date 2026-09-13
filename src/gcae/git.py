from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path


class GitError(RuntimeError):
    """Raised when a Git safety invariant or command fails."""


class GitRepository:
    def __init__(
        self,
        source: str | Path,
        runtime_dir: str | Path,
        worktree_dir: str | Path | None = None,
    ) -> None:
        self.source = Path(source).resolve()
        self.runtime_dir = Path(runtime_dir).resolve()
        self.worktree_dir = (
            Path(worktree_dir).resolve()
            if worktree_dir is not None
            else self.runtime_dir / "worktrees"
        )
        self.worktree: Path | None = None
        self.branch: str | None = None

    def _run(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd or self.source,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise GitError(result.stderr.strip() or result.stdout.strip() or "git command failed")
        return result.stdout.strip()

    def validate_source(self) -> str:
        if not self.source.is_dir():
            raise GitError(f"source is not a directory: {self.source}")
        if self._run("rev-parse", "--is-inside-work-tree", check=False) != "true":
            raise GitError(f"source is not a Git repository: {self.source}")
        for directory in (self.runtime_dir, self.worktree_dir):
            if directory == self.source or self.source in directory.parents:
                raise GitError("runtime directories must be external to the source repository")
        status = self._run("status", "--porcelain", "--untracked-files=all")
        if status:
            raise GitError("source repository has uncommitted changes; refusing to mutate it")
        return self._run("rev-parse", "HEAD")

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
        output = self._run("status", "--porcelain", cwd=self._require_worktree())
        return [line[3:] for line in output.splitlines() if len(line) >= 4]

    def status_entries(self) -> list[tuple[str, str]]:
        output = self._run("status", "--porcelain", cwd=self._require_worktree())
        return [(line[:2], line[3:]) for line in output.splitlines() if len(line) >= 4]

    def checkpoint(self, message: str) -> str:
        worktree = self._require_worktree()
        self._run("add", "-A", cwd=worktree)
        if not self.status():
            raise GitError("cannot create checkpoint with no changes")
        self._run("commit", "-m", message, cwd=worktree)
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
