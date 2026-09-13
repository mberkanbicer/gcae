# Git execution

The source repository must be a Git repository with a committed base. When it is not — unborn
`HEAD`, or a dirty working tree — GCAE creates that base commit itself before the run (see *Base
commit bootstrap* below); pass `--no-auto-bootstrap` to refuse instead. It
creates exactly one external worktree and branch per run. Runtime-owned rollback performs
`reset --hard <accepted_commit>` and `clean -fdx` only inside that worktree. The source branch is
never merged or modified.

## Base commit bootstrap

A run's worktree is created from a base commit. Rather than refusing, GCAE prepares the repository:

| Situation | Action |
| --- | --- |
| `git init`, no commits, no files | creates an empty base commit (`--allow-empty`) |
| `git init`, no commits, files present | commits the working tree as `gcae: base commit of the current working tree` |
| commits present, working tree dirty | commits the pending changes as the base commit |
| no `user.name`/`user.email` anywhere | commits with `GCAE <gcae@localhost>` and reports a notice |
| mid-merge / cherry-pick / revert / rebase | **refused**, naming the operation |
| more than 2000 files or 50 MB pending | **refused**, with the file count and size |
| `--no-auto-bootstrap` or `[runtime] auto_bootstrap = false` | **refused**, with the manual command |

Guarantees: file contents are never modified, ignored files stay untracked, nothing else is staged,
and the base commit is reported in the CLI log and in the dashboard timeline. It is an ordinary
commit on the current branch, so `git reset --soft HEAD~1` undoes it.

Only evaluator acceptance creates `gcae: <goal>` checkpoint commits. A passing final verification
with a non-empty worktree creates the final `gcae: verified final state` checkpoint, so
`accepted_commit` always equals the verified tree of a completed run. Resume verifies that the
persisted worktree is registered with the source repository before resetting it.

A completed run is merged into the source branch so the work is visible in the checkout.
`[runtime] auto_merge` (default `true`) controls this; `--merge` forces it, `--no-merge` disables it,
and with `auto_merge = false` an interactive CLI asks. The dashboard merges off the UI thread and
offers `M` when the run is complete and unmerged. The same merge is available later as
`gcae merge <repository> <run-id>`. Every path applies the same guards: the run must be complete, not
already merged, and its branch must still point at the verified commit; the source repository must
be clean. A run that produced no changes reports "nothing to merge" instead of failing. The merge requires a clean
source repository, prefers `--ff-only`, falls back to `--no-ff`, and aborts cleanly on conflicts.
The pre-merge and merge commits are recorded in `state.json`, and `gcae undo <repository> <run-id>`
resets the source branch back to the recorded pre-merge commit, refusing when the repository is
dirty or HEAD moved after the merge.

Before validation and final verification, runtime-owned cleanup removes generated Python/test
caches and ignored files from the isolated worktree only. This prevents test artifacts from being
checkpointed while preserving the source repository.
