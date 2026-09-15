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


class NothingToMerge(GitError):
    """A completed run produced no file changes, so there is no merge to perform."""


class MergeConflict(GitError):
    """The merge stopped on conflicting edits; ``files`` lists the unresolved paths."""

    def __init__(self, message: str, files: list[str] | None = None) -> None:
        super().__init__(message)
        self.files = list(files or [])


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

    def prune_worktrees(self) -> None:
        """Forget worktrees whose directory no longer exists (GCAE's own bookkeeping)."""
        self._run("worktree", "prune", check=False)

    def registered_worktrees(self) -> set[Path]:
        listing = self._run("worktree", "list", "--porcelain", check=False)
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in listing.splitlines()
            if line.startswith("worktree ")
        }

    def ensure_worktree(self, branch: str, path: Path) -> Path:
        """Create or re-register the worktree for a branch (used by resume).

        GCAE owns the runtime directory, so a directory left behind without a
        registration is cleaned up instead of blocking the next run.
        """
        self.prune_worktrees()
        path = Path(path).resolve()
        registered = self.registered_worktrees()
        if path.exists() and path not in registered:
            shutil.rmtree(path)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._run("worktree", "add", str(path), branch)
        self.worktree = path
        self.branch = branch
        return path

    def create_isolated_worktree(self, run_id: str | None = None) -> tuple[Path, str, str]:
        base = self.validate_source()
        self.prune_worktrees()
        run_id = run_id or uuid.uuid4().hex[:12]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id):
            raise GitError("run_id contains unsupported characters")
        branch = f"gcae/{run_id}"
        worktree = self.worktree_dir / run_id
        if worktree.exists():
            if worktree.resolve() in self.registered_worktrees():
                raise GitError(f"worktree is in use by a live run: {worktree}")
            shutil.rmtree(worktree)
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

    def commit_subject(self, commit: str) -> str:
        """One-line subject of a commit, used to explain reconciliation to the user."""
        return self._run("log", "-1", "--format=%s", commit)

    def is_ancestor(self, commit: str, descendant: str) -> bool:
        """True when ``commit`` is an ancestor of ``descendant`` (or equal).

        Used to reconcile plan history with rollback targets: steps completed at
        checkpoints that remain reachable stay valid; the rest are affected.
        """
        if not commit or not descendant:
            return False
        if commit == descendant:
            return True
        try:
            self._run("merge-base", "--is-ancestor", commit, descendant)
        except GitError:
            return False
        return True

    def current_branch(self) -> str:
        return self._run("rev-parse", "--abbrev-ref", "HEAD")

    def source_commit(self) -> str:
        return self._run("rev-parse", "HEAD")

    def unresolved_paths(self, cwd: Path | None = None) -> list[str]:
        output = self._run(
            "diff", "--name-only", "--diff-filter=U", cwd=cwd or self.source, check=False
        )
        return [line for line in output.splitlines() if line.strip()]

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
                files = self.unresolved_paths()
                self._run("merge", "--abort", check=False)
                if files:
                    raise MergeConflict(
                        f"merging {branch} conflicts in {len(files)} file(s)", files
                    ) from exc
                raise GitError(f"cannot merge {branch}: {exc}") from exc
        merged = self.source_commit()
        if merged == pre:
            raise GitError(f"branch {branch} is already merged")
        return pre, merged

    def stage_all(self) -> None:
        """Stage the worktree (also clears git's unmerged index entries after a fix)."""
        self._run("add", "-A", cwd=self._require_worktree())

    def conflict_marker_files(self, limit: int = 1_000_000) -> list[str]:
        """Worktree files that still contain conflict markers.

        Content is the truth here: staging a file resolves the *index*, not the markers,
        and the model is not allowed to run git itself.
        """
        candidates: set[str] = set(self.unresolved_paths(self.worktree))
        candidates.update(path for _, path in self.status_entries())
        worktree = self._require_worktree()
        marked: list[str] = []
        for relative in sorted(candidates):
            target = worktree / relative
            try:
                if not target.is_file() or target.stat().st_size > limit:
                    continue
                content = target.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "\n<<<<<<< " in f"\n{content}" and "\n>>>>>>> " in f"\n{content}":
                marked.append(relative)
        return marked

    def merge_into_worktree(self, target: str) -> list[str]:
        """Merge ``target`` into the run branch *inside its worktree*.

        Returns the conflicting paths, leaving the merge in progress so the agent can
        resolve the markers; an empty list means the branch now contains ``target``.
        """
        worktree = self._require_worktree()
        if self.status():
            raise GitError("worktree has uncommitted changes; cannot start a merge")
        try:
            self._run("merge", "--no-ff", "--no-edit", target, cwd=worktree)
        except GitError:
            return self.unresolved_paths(worktree)
        return []

    def worktree_merge_in_progress(self) -> bool:
        worktree = self._require_worktree()
        return bool(
            self._run("rev-parse", "--verify", "--quiet", "MERGE_HEAD", cwd=worktree, check=False)
        )

    def abort_worktree_merge(self) -> None:
        worktree = self._require_worktree()
        self._run("merge", "--abort", cwd=worktree, check=False)

    def undo_merge(self, pre_merge_commit: str, merge_commit: str) -> None:
        """Reset the source branch to the recorded pre-merge commit."""
        if self._run("status", "--porcelain", check=False):
            raise GitError("source repository has uncommitted changes; refusing to undo merge")
        if self.source_commit() != merge_commit:
            raise GitError("source HEAD moved since the merge; refusing to undo")
        self._run("reset", "--hard", pre_merge_commit)

    def assert_registered_worktree(self) -> None:
        worktree = self._require_worktree().resolve()
        if worktree not in self.registered_worktrees():
            raise GitError(f"worktree is not registered with the source repository: {worktree}")

    def current_commit(self) -> str:
        return self._run("rev-parse", "HEAD", cwd=self._require_worktree())

    def status(self) -> str:
        return self._run("status", "--porcelain", cwd=self._require_worktree())

    def diff(self) -> str:
        """Candidate diff: the working tree against the last checkpoint, untracked files included.

        A bare ``git diff`` hides two kinds of change the agent must see: untracked files
        (observed: a complete 30-line script rewritten six times because the agent could not see
        the file it had just created) and staged changes. Both made the agent reason about a
        repository that did not match reality.
        """
        worktree = self._require_worktree()
        if not any(code == "??" for code, _ in self.status_entries()):
            return self._run("diff", "HEAD", "--no-ext-diff", cwd=worktree)
        return "\n".join(item["diff"] for item in self.diff_by_file())

    def diff_stat(self) -> str:
        return self._run("diff", "HEAD", "--stat", cwd=self._require_worktree())

    def diff_check(self) -> bool:
        return (
            self._run("diff", "HEAD", "--check", cwd=self._require_worktree(), check=False) == ""
        )

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

    def files_between(self, base_ref: str, other_ref: str) -> list[str]:
        """Files that differ between two refs — exactly what a merge would bring."""
        if not base_ref or not other_ref:
            return []
        output = self._run("diff", "--name-only", base_ref, other_ref, check=False)
        return [line for line in output.splitlines() if line.strip()]

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
                # HEAD keeps staged changes visible in the candidate view
                text = self._run("diff", "HEAD", "--no-ext-diff", "--", path, cwd=worktree)
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
        merging = self.worktree_merge_in_progress()
        if not self.status() and not merging:
            raise GitError("cannot create checkpoint with no changes")
        args = [*self._identity_args(), "commit", "-m", message]
        if merging:
            # even when the resolved tree equals one side, the merge commit must record
            # both parents or the merge stays in progress forever
            args.append("--allow-empty")
        self._run(*args, cwd=worktree)
        return self.current_commit()

    def rollback(self, accepted_commit: str) -> None:
        worktree = self._require_worktree()
        if self._run("rev-parse", "--verify", "--quiet", "MERGE_HEAD", cwd=worktree, check=False):
            # reset alone leaves MERGE_HEAD behind, which would make the next commit a merge
            self._run("merge", "--abort", cwd=worktree, check=False)
        self._run("reset", "--hard", accepted_commit, cwd=worktree)
        self._run("clean", "-fdx", cwd=worktree)
        if self.current_commit() != accepted_commit or self.status():
            raise GitError("rollback failed to restore clean accepted checkpoint")

    def remove_worktree(self) -> None:
        if self.worktree is None:
            return
        self._run("worktree", "remove", "--force", str(self.worktree), check=False)
        if self.worktree.exists():
            shutil.rmtree(self.worktree)
        self.prune_worktrees()
        self.worktree = None

    def branch_exists(self) -> bool:
        return self.branch is not None and bool(
            self._run("show-ref", "--verify", f"refs/heads/{self.branch}", check=False)
        )
