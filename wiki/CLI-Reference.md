# CLI Reference

```
gcae [-h] [--version] {run,resume,list,inspect,undo,merge} ...
```

## Configuration discovery

Without `--config`, GCAE uses `$GCAE_CONFIG`, then `./config.toml`, then
`~/.config/gcae/config.toml`, and prints the file it took (`gcae: using config config.toml`). With none
present the built-in defaults apply — including the *fake* provider, which is why `run` and `resume`
say so instead of failing later.

## `gcae run`

```
gcae run <repository> [request] [--request TEXT | -p TEXT] [--config FILE] [--runtime-dir DIR]
         [--constraint TEXT]... [--criterion TEXT]...
         [--headless | --tui] [--merge | --no-merge] [--no-auto-bootstrap]
```

| Argument | Meaning |
| --- | --- |
| `request` / `--request` / `-p` | the task in words; either spelling works (both is an error) |

A request on the command line runs headlessly — the task is already stated, so the work just
happens and the summary prints at the end. Omit the request in a terminal to be asked
interactively, or pass `--tui` to open the dashboard with it pre-filled.
| `--criterion` | a checkable success criterion (repeatable) — see [Concepts](Concepts) |
| `--constraint` | a hard constraint the work must respect (repeatable) |
| `--headless` / `--tui` | force non-interactive output, or force the dashboard |
| `--merge` / `--no-merge` | override the merge decision for this run |
| `--no-auto-bootstrap` | refuse to create the base commit a run needs |
| `--runtime-dir` | where runs, worktrees and memory live |

## `gcae resume`

```
gcae resume <repository> <run-id> [--headless | --tui] [--merge | --no-merge] [--force]
```

Continues a run that stopped, failed or is `waiting_for_user`. The worktree is recreated from the run
branch and the accepted commit is restored before the loop resumes. Whatever a crash left behind is
reconciled and *reported* (`resume_reconciled` notices: discarded unrecorded checkpoint, branch
divergence, torn event line, plan invalidation). A `blocked` or `waiting_for_user` run is **held**,
not restarted — the CLI prints the reason and hint; `--force` overrides openly, and an instruction
(TUI `i`) answers it instead.

## `gcae list`

Runs from newest to oldest with id, status, accepted steps, update time and objective.

## `gcae inspect`

```
gcae inspect <repository> <run-id> [--json]
```

Objective, plan and step status, verification per criterion, merge record, pending question, the
recovery diagnosis (root cause and correction) and any `degraded:` subsystems. `--json` prints the
persisted state verbatim.

## `gcae prune`

Deletes the oldest run records from the state directory, keeping the newest `--keep` (default 10).
The whole run directory goes (state and events); repositories and worktrees are never touched.

```
gcae prune --dry-run    # list what would be deleted
gcae prune --keep 20    # keep the 20 newest, delete the rest
gcae prune --keep 0     # delete every run record
gcae prune --older-than 30  # also delete runs older than 30 days, even within --keep
```

`[runtime] run_retention_days` applies the same TTL without the flag (the flag wins when both
are given). Pruning removes run directories only — the cumulative knowledge database
(`memory.db`) is deliberately left alone: the per-run record goes, the lesson stays.

Three guards keep pruning from breaking anything:

- a run whose repository lock is held is never deleted — that is a live run in another process,
  however old its record is;
- a record whose merge is still recorded is kept — it holds the pre-merge/merge commit pair that
  `gcae undo` reverses from, and git has it nowhere else; `--force` deletes it anyway;
- unreadable records are reported and skipped, never silently kept or deleted.

## `gcae merge` / `gcae undo`

```
gcae merge <repository> <run-id>          # bring accepted commits into the checkout
gcae undo <repository> <run-id>           # reverse a recorded merge (the branch stays)
```

A merge that conflicts is handed to the agent to resolve and re-verify before it is applied.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | the run finished and verified (`status == "complete"`) |
| `1` | a handled error (bad config, refused repository, refused merge) or a run that ended `failed`, `stopped` or `waiting_for_user` |
| `2` | argparse usage error |

Scripted callers can rely on the exit status; a run that ends `waiting_for_user` exits non-zero even
though its accepted work is intact.

## Reading a summary

```
gcae: using config config.toml
...
gcae: full result in result.json
run 2f4ac1b0c3e9: complete
  accepted steps: 1, commit: 5c01d9ab1e2f
  verification  : 1/1 criteria passed
  worktree      : ~/.local/state/gcae/worktrees/2f4ac1b0c3e9
  branch        : gcae/2f4ac1b0c3e9 merged into main (undo: gcae undo /repo 2f4ac1b0c3e9)
  files         : app.py
  documents     : /repo (in your working tree now)
```

| Line | Meaning |
| --- | --- |
| `gcae: using config <file>` | which configuration was used |
| `question:` / `answer with:` | the run needs your decision; answer with `gcae resume` or `i` in the dashboard |
| `degraded: …` | a subsystem failed and the run continued without it |
| `gcae: error: …` | the failure reason; accepted commits are still on the branch |
| `gcae: unexpected error: …` | an unhandled bug — the run directory and commits are intact |
