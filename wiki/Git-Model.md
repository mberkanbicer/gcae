# Git Model

## Branches and worktrees

Each run creates exactly one branch `gcae/<run-id>` and one worktree
(`<state_dir>/worktrees/<run-id>`) from a committed base. The agent edits only the worktree. Your
checkout changes only when the run is merged, and that merge is recorded.

## Who does the Git work

The loop does — you never have to run `git` for GCAE to work:

| Situation | What GCAE does |
| --- | --- |
| `git init` with no commits | creates the base commit (`--allow-empty` when the tree is empty) |
| uncommitted changes before a run | commits them as `gcae: base commit of the current working tree` — never discards them |
| uncommitted changes found at merge time | commits them as the base the merge builds on |
| no `user.name` / `user.email` | commits as `GCAE <gcae@localhost>` |
| completed run | merges the verified branch into your current branch |
| run that failed after accepting steps | merges that accepted work, labelled unverified |
| conflicting merge | aborts it in your checkout, replays it inside the run worktree, has the agent resolve the markers, re-verifies, retries the merge |
| worktree after merging | removed by GCAE; the branch is kept so `gcae undo` and re-merging still work |
| worktree deleted by hand | pruned or recreated from the run branch |
| your repository is mid-merge / rebase / cherry-pick | **refused** — that state belongs to you |

## Checkpoints

Only an accepted evaluation creates a commit:

```
gcae: <goal>            one accepted semantic step
gcae: verified final state   the final tree after verification, when files changed
```

`accepted_commit` in `state.json` is therefore always a tree that passed validation, evaluation and
verification. Rollback is `reset --hard <accepted_commit>` plus `clean -fdx`, scoped to the worktree.

## Merge and undo

Merging prefers `--ff-only` and falls back to `--no-ff`. The pre-merge and merge commits are recorded
in `state.json`:

```bash
gcae merge ~/src/project <run-id>     # merge later, resolving conflicts through the agent
gcae undo  ~/src/project <run-id>     # reset the branch to the recorded pre-merge commit
```

`gcae undo` refuses when the source repository is dirty or when HEAD moved after the merge, so it
cannot silently discard unrelated work. Undo does **not** delete the run branch, so the merge can be
redone.

## Untracked and generated files

- `.gitignore` is respected everywhere: ignored files are never committed by bootstrap or checkpoint.
- Generated caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`, `*.pyc`)
  are cleaned before validation and again after verification, so they can never reach a checkpoint.
- Untracked directories are reported at file level, which is what makes scope checks and `+N -M`
  counts accurate.

## Bootstrap limits

Automatic base commits are bounded: more than 2000 files or 50 MB of pending changes is refused with
the count and size, so a repository full of unignored build output cannot be swallowed silently.
`--no-auto-bootstrap` (or `[runtime] auto_bootstrap = false`) restores manual control.
