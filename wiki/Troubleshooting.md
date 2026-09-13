# Troubleshooting

## The run stops by itself

| Status | Meaning | What to do |
| --- | --- | --- |
| `waiting_for_user` | the agent ran out of ideas or needs a decision | answer it: `i` in the dashboard, or `gcae resume <repo> <run-id>` |
| `failed: execution stagnated after asking` | you were asked already and nothing changed | `gcae resume` after an instruction, or start a narrower task |
| `failed: step budget exhausted after N iterations` | the model kept working without finishing | raise `[runtime] max_steps` or give a smaller task |
| `failed: provider output` | the model returned something unusable repeatedly | see [Providers](Providers) |
| `failed: merge conflict unresolved` | the agent could not resolve a conflicting merge | the branch is intact; fix the conflict and run `gcae merge` |

In every case the accepted checkpoints survive, and they are merged into your checkout (labelled
unverified when the final verification did not pass).

## "Nothing happened" after the run

Read the last two summary lines:

```
files: parser.py
documents: ~/src/project (in your working tree now)
```

`documents:` names the folder that holds the result. While a run is unmerged, the files physically
live in `~/.local/state/gcae/worktrees/<run-id>` on the run branch.

## GCAE refuses to start

| Message | Meaning |
| --- | --- |
| `source is a subdirectory of a Git repository; pass the repository root: …` | pass the repository root |
| `source repository has an in-progress merge; finish or abort it …` | that state is yours to finish |
| `source repository has N uncommitted files (X MB) — GCAE will not auto-commit that much` | commit, stash, or ignore the files first |
| `runtime directories must be external to the source repository` | move `state_dir`/`worktree_dir` outside the repository |

## Merge did not happen

```bash
gcae inspect <run-id>            # shows the merge record, or the absence of one
gcae merge ~/src/project <run-id>
```

Common causes: the repository was dirty (GCAE commits pending edits as the merge base), the branch
moved past the accepted commit (refused on purpose), or the merge conflicted (handed to the agent).

## A run cannot be resumed

`gcae resume` recreates a missing worktree from the run branch. It fails only when the branch itself
is gone, for example after you deleted it; in that case the recorded merge commit in your own history
(or `git log --all --grep gcae`) is the remaining trace.

## The dashboard shows `measuring…` or empty panels

Git status is collected in a worker thread; on very large repositories it can lag a moment. `d`
(diff), `l` (logs) and `c` (context) are authoritative.

## Still stuck?

1. `gcae inspect <run-id> --json` — the persisted truth for that run.
2. `runs/<run-id>/events.jsonl` — every event, in order, with payloads.
3. `runs/<run-id>/tool-results/` — the exact tool outputs, including full command output.
4. `DEBUG` logging: `PYTHONUNBUFFERED=1 gcae run … 2>&1 | tee run.log` (the CLI sets the `gcae` logger
   to INFO; library users can raise it).
