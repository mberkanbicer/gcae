# Git execution

The source repository must be a Git repository with a committed base. When it is not — unborn
`HEAD`, or a dirty working tree — GCAE creates that base commit itself before the run (see *Base
commit bootstrap* below); pass `--no-auto-bootstrap` to refuse instead. It
creates exactly one external worktree and branch per run. Runtime-owned rollback performs
`reset --hard <accepted_commit>` and `clean -fdx` only inside that worktree. The source branch is
changed only by the recorded merge (fast-forward or merge commit), which `gcae undo` reverses.

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
`accepted_commit` always equals the verified tree of a completed run. Resume re-registers or recreates the
persisted worktree from the run branch instead of failing when it is missing.

## Who does the git work

The loop owns it; the user never has to run git:

| Requirement | Handled by |
| --- | --- |
| repository without commits | base commit created (`--allow-empty` when empty) |
| uncommitted working tree before a run | committed as `gcae: base commit of the current working tree` |
| uncommitted working tree found at merge time | committed as the base the merge builds on |
| missing `user.name`/`user.email` | commits as `GCAE <gcae@localhost>` |
| branch, worktree, checkpoints | created and owned by the runtime |
| merge of completed runs | automatic (`auto_merge`) |
| merge of a failed run's accepted checkpoints | automatic (`merge_accepted_on_failure`), labelled unverified |
| worktree after a merge | removed by GCAE; the branch is kept so `gcae undo` and re-merging still work |
| worktree deleted by hand | pruned/recreated on the next run or resume |
| conflicting merge | the agent resolves it inside the run worktree (`resolve_merge_conflicts`), the run re-verifies, then the merge is retried |
| mid-merge / rebase / cherry-pick state | **refused** — that state belongs to the user |

### Conflict resolution

A conflicting merge is not a dead end and never touches the user's checkout:

1. the runtime detects the conflict (`MergeConflict` names the files) and aborts the source-side
   merge, leaving the checkout untouched;
2. it merges the target into the run branch **inside the run's worktree**, so the conflict markers
   appear where the agent works and nothing of the user's is modified;
3. the agent gets an ordinary semantic step (`resolve the merge conflict in <files>`, scope = those
   files), fixes them with its normal tools, and the runtime stages the result (the model never runs
   git);
4. acceptance creates the merge commit — with `--allow-empty` when the resolved tree matches one
   side, because the commit's two parents are what record the merge — and the run is verified again;
5. the outer merge then fast-forwards, and `gcae undo` still reverses it.

If the agent fails, the in-worktree merge is aborted, the branch is left exactly as it was, and the
run is marked `failed: merge conflict unresolved` — conflict markers are never committed or merged.
Set `[runtime] resolve_merge_conflicts = false` to skip step 2–5 and keep the manual path.

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
