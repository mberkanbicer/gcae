"""Guarded merge of a run branch into the source branch.

Deliberately runtime-free: these functions take explicit inputs (a GitRepository and the
run's state) so both the runtime loop and ``gcae merge`` apply identical checks. Loop
control stays in ``runtime.py``; this module only owns the merge rules.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .git import GitError, GitRepository, NothingToMerge
from .models import AgentState, MergeRecord, PendingMerge

logger = logging.getLogger(__name__)


def cleanup_idle_worktree(repo: GitRepository, state: AgentState) -> bool:
    """Remove GCAE's own worktree once the run no longer needs it. True when removed.

    Used when the run's work is delivered (merged) or empty (nothing to merge). The branch
    is deliberately kept when a merge happened: the merge stays reversible with
    ``gcae undo`` and the accepted commits can be merged again.
    """
    path = Path(state.worktree)
    if not path.exists():
        return False
    repo.worktree = path
    repo.remove_worktree()
    return True


def cleanup_merged_worktree(repo: GitRepository, state: AgentState) -> bool:
    """Remove the worktree of a merged run (see :func:`cleanup_idle_worktree`)."""
    if state.merge is None:
        return False
    return cleanup_idle_worktree(repo, state)


def merge_verified_run(
    repo: GitRepository,
    state: AgentState,
    persist: Callable[[], None],
    allow_unverified: bool = False,
) -> MergeRecord:
    """Guarded merge of a run branch into the current source branch.

    Shared by the runtime (after completion) and ``gcae merge`` so both paths apply the
    same checks: the run must be complete, unmerged, and its branch must still point at
    the verified commit; the source repository must be clean.

    ``allow_unverified`` additionally permits a failed or stopped run that accepted at
    least one checkpoint: the user asked for the accepted work back explicitly, and the
    caller reports that final verification did not pass.
    """
    unverified = state.status != "complete"
    if unverified and not (allow_unverified and state.accepted_steps > 0):
        raise RuntimeError(f"run {state.run_id} is not complete: {state.status}")
    if unverified and state.merge is None:
        logger.warning(
            "merging accepted work from run %s without final verification (%s)",
            state.run_id,
            state.status,
        )
    if state.merge is not None:
        raise RuntimeError(
            f"run {state.run_id} is already merged into {state.merge.target_branch}; "
            f"run 'gcae undo {state.source_repo} {state.run_id}' first"
        )
    if state.pending_merge is not None and state.branch:
        # crash-window marker: a merge was intended. If it already happened, the
        # source branch carries the merge and the record is backfilled from git —
        # never merged twice, never left without an undo record.
        marker = state.pending_merge
        try:
            merged_already = (
                marker.target_branch == repo.current_branch()
                and repo.source_commit() != marker.pre_merge_commit
                and repo.is_ancestor(state.branch, repo.source_commit())
            )
        except GitError:
            merged_already = False
        if merged_already:
            record = MergeRecord(
                branch=marker.branch,
                target_branch=marker.target_branch,
                pre_merge_commit=marker.pre_merge_commit,
                merge_commit=repo.source_commit(),
            )
            state.merge = record
            state.pending_merge = None
            persist()
            return record
        if marker.branch != state.branch or marker.target_branch != repo.current_branch():
            # stale marker from a different target: forget it and merge fresh
            state.pending_merge = None
            persist()
    if not state.branch:
        raise RuntimeError(f"run {state.run_id} has no branch to merge")
    if state.accepted_commit:
        head = repo.branch_head(state.branch)
        if head != state.accepted_commit:
            raise RuntimeError(
                f"branch {state.branch} moved past the verified commit "
                f"{state.accepted_commit[:12]}; refusing to merge"
            )
    if state.branch and repo.branch_head(state.branch) == repo.source_commit():
        raise NothingToMerge(
            f"run {state.run_id} produced no file changes; there is nothing to merge"
        )
    if repo.source_status_entries():
        # GCAE never leaves the user to stash their own work: commit it as the base the
        # merge builds on (bounded, reported, reversible with git reset --soft HEAD~1).
        repo.bootstrap_source_repository()
    target = repo.current_branch()
    # persist intent before the merge: a crash between `git merge` and the record
    # write is then reconcilable by ancestry on the next attempt
    state.pending_merge = PendingMerge(
        branch=state.branch, target_branch=target, pre_merge_commit=repo.source_commit()
    )
    persist()
    pre, merged = repo.merge_branch(state.branch)
    record = MergeRecord(
        branch=state.branch,
        target_branch=target,
        pre_merge_commit=pre,
        merge_commit=merged,
    )
    state.merge = record
    state.pending_merge = None
    persist()
    return record
