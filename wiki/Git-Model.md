# Git Model

## Branches and worktrees

| Thing | Name | Notes |
| --- | --- | --- |
| Run branch | `gcae/<run-id>` | created from the current `HEAD` of the source branch |
| Worktree | `<state_dir>/worktrees/<run-id>` | exactly one per run, never inside your repository |
| Base commit | your `HEAD` | recorded as the run's starting point |
| Checkpoint | `gcae: <goal>` | one commit per accepted step |
| Final commit | `gcae: verified final state` | only if the final tree was dirty |

## Who does the Git work

The runtime. Base commit, commit identity fallback, branch, worktree, checkpoints, merges, conflict
resolution, cleanup and undo are all performed by GCAE; there is no workflow that requires you to run
`git`, and the model cannot: `git` subcommands such as `reset`, `clean`, `commit`, `worktree`,
`checkout` and `merge` are blocked in `run_command`.

## One run per repository

`run`, `resume`, `merge` and `undo` take a per-repository lock (`<state_dir>/locks/<digest>.lock`)
before they touch the source branch. A second run is refused with the id of the run holding it:

```
gcae: error: another GCAE run is already working on /repo (run 46aa6c93aa43) — wait for it to
finish before starting a second run on the same repository
```

The lock lives in the state directory, not in the repository, and the operating system releases it if
the process dies. Runs on different repositories never block each other.

## Checkpoints

An accepted step commits everything the step changed — tracked, staged and untracked — as
`gcae: <goal>`. That commit becomes `accepted_commit`, the only tree a rejection resets to. A
rejection never touches an earlier accepted commit.

## Merge and undo

When a run completes, its branch is merged into the source branch (fast-forward when possible) and
the merge is recorded in `state.json` (`pre_merge_commit`, `merge_commit`, `target_branch`):

```
gcae: merged gcae/2f4ac1b0c3e9 into main (b21f0aa1 -> 5c01d9ab)
gcae: undo with: gcae undo /repo 2f4ac1b0c3e9
```

| Flag | Effect |
| --- | --- |
| `--merge` | merge a verified branch without asking |
| `--no-merge` | never merge this run's branch (`[runtime] auto_merge = false` makes this the default) |
| `[runtime] merge_accepted_on_failure` | a failed or stopped run may still bring its accepted commits in |

If the merge cannot happen — dirty checkout, branch moved, nothing to merge — the reason is printed
and the branch stays for `gcae merge <repo> <run-id>`.

## Conflict resolution

A conflicting merge is brought into the run's own worktree, the agent resolves the conflicted files
as a normal semantic step, the result is re-verified and the merge is retried. Merge markers are
never committed and never pushed into the source branch. If resolution fails, the branch is kept
intact and reported.

## Untracked and generated files

The candidate diff is `git diff HEAD` plus untracked files, so the agent sees what it actually wrote.
Generated and ignored artifacts are cleaned before a checkpoint: they never enter a commit, and
`clean -fdx` on rollback stays inside the worktree.

## Bootstrap limits

When a repository needs a base commit, GCAE stages what `.gitignore` allows, bounded to 2000 files
and 50 MB, and reports it (`created a base commit …`). A repository mid-merge or mid-rebase is
refused with an explanation rather than modified.
