# CLI

```
gcae run <repository> [<request>] [--config PATH] [--runtime-dir PATH]
         [--constraint TEXT]... [--criterion TEXT]... [--merge|--no-merge]
         [--no-auto-bootstrap] [--tui|--headless]
gcae resume <repository> <run-id> [--config PATH] [--runtime-dir PATH] [--tui|--headless]
gcae list [--config PATH] [--runtime-dir PATH]
gcae inspect <run-id> [--config PATH] [--runtime-dir PATH] [--json]
gcae merge <repository> <run-id> [--config PATH] [--runtime-dir PATH]
gcae undo <repository> <run-id> [--config PATH] [--runtime-dir PATH]
```

The request is optional in TUI mode: `gcae run <repository>` opens the TUI and asks for the task,
and the planner derives the success criteria from it. Headless mode requires a request and exits 1
without one.

## Preconditions (checked with actionable errors)

- the path is the **repository root** (a subdirectory is refused with the root path);
- the repository has a base commit and a clean working tree — GCAE creates that commit itself when
  it is missing or the tree is dirty (`--no-auto-bootstrap` refuses instead; `.gitignore` respected,
  bounded to 2000 files / 50 MB, file contents never modified);
- `git user.name` and `git user.email` resolve (repo, global or system config); otherwise GCAE
  commits with `GCAE <gcae@localhost>` and reports it;
- `git` is installed and on `PATH`;
- remote HTTP providers have an API key (`api_key` or `api_key_env`); local endpoints do not need one;
- the runtime state directory is writable.

`planner failed` means the model-backed planner could not produce a plan. When you supplied
`--criterion`, GCAE falls back to a deterministic single-step plan and says so in the timeline
(`! planner unavailable · single-step plan · <reason>`); without criteria it stops and tells you the
exact `--criterion` to add. Provider failures report the underlying cause (connection refused, HTTP status, invalid JSON)
instead of a generic message. `run` and `resume` open the dashboard on a terminal;
non-interactive environments (or `--headless`) get the event log on stderr plus the final
`AgentState` JSON on stdout. The summary line reports status, accepted steps, verification,
worktree and branch/merge state.

## Criteria

Criteria are verified deterministically; unsupported criteria fail closed unless
`[verifier] kind = "hybrid"` is configured.

- `file exists: path/to/file`
- `file contains: path/to/file :: expected text`
- `file contains exactly: path/to/file :: expected text` (a trailing newline at end of file is
  ignored; anything else must match exactly)
- `command succeeds: pytest -q`

When no `--criterion` is given, the planner derives criteria from the request (the model-backed
planner is required to produce at least one). `--criterion` values are merged with inferred ones.

## Configuration discovery

Without `--config`, GCAE uses `$GCAE_CONFIG`, then `./config.toml`, then
`~/.config/gcae/config.toml`, and prints which one it took. With none present, `run` and `resume`
warn that the built-in fake provider is about to be used instead of failing cryptically later.

## Runs

- `list` prints known runs (id, status, accepted steps, update time, objective), newest first.
- `inspect` prints objective, plan, verification per criterion, merge state and pending question;
  `--json` emits the persisted state verbatim.
- `resume` reconciles whatever a crash left behind and says so (`resume_reconciled` notices:
  discarded unrecorded checkpoint, branch divergence, torn event line, plan invalidation).
  A `blocked` or `waiting_for_user` run is held, not restarted: the CLI prints the reason
  and hint; `--force` overrides openly (recorded as `resume_forced`), and an instruction
  (TUI `i`) answers it instead.

## Prune

Run records accumulate under the state directory (`runs/<run-id>/` each: state, events, worktree
history). `prune` deletes the oldest records, keeping the newest `--keep` (default 10):

```
gcae prune --dry-run                 # list what would be deleted
gcae prune --keep 20                 # keep the 20 newest, delete the rest
gcae prune --keep 0                  # delete every run record
gcae prune --older-than 30           # also delete runs older than 30 days, even within --keep
```

`[runtime] run_retention_days` applies the same TTL without the flag (the flag wins when both
are given).

Pruning removes run records only — it never touches repositories or worktrees, and it never
deletes a run whose repository lock is held: that is a live run in another process, however old its
record is. The shared knowledge database is deliberately left alone: `memory.db` (memory records
and the evidence ledger) is cumulative knowledge, so pruning a run's directory does not delete
what that run taught — the per-run record goes, the lesson stays. Two further guards keep pruning from breaking anything: a record whose merge is still
recorded is kept (it holds the pre-merge/merge commit pair that `gcae undo` reverses from) unless
`--force` says otherwise, and unreadable records are reported and skipped, not silently kept or
deleted.

## Merge / undo

A completed run is merged into the branch you currently have checked out, so the work is visible in
your working tree. `[runtime] auto_merge` (default `on`) controls this; `--no-merge` opts out and
keeps the branch separate, and `--merge` forces it. With `auto_merge = false` an interactive CLI
asks instead of merging.

A **failed or stopped** run can still be rescued: `gcae merge` merges the checkpoints it already
accepted (with a warning that final verification did not pass), and the dashboard offers `M` labelled
*Merge accepted work*. Automatic merging only ever applies to completed runs.

You never need to run git yourself: the loop creates the base commit and the commit identity,
merges on completion, merges the accepted checkpoints of a failed run, commits a dirty checkout as
the base the merge builds on, and removes its own worktree afterwards (keeping the branch so
`gcae undo` still works).

A conflicting merge is resolved by the agent: GCAE merges your branch into the run branch inside
the run's own worktree, hands the conflicting files to the loop as a step, re-verifies, and merges
again once the conflict is gone. Conflict markers never reach your checkout.

Every merge path applies the same guards — the run must be complete, not already merged, and its
branch must still point at the verified commit; the source repository must be clean. A run with no
file changes reports `nothing to merge` rather than failing.

The end-of-run summary always says where the documents are: `files: docs/api.md, docs/guide.md`
plus `documents: /path/to/repo (in your working tree now)` once merged, or
`documents: …/worktrees/<run-id> (worktree; nothing is in your checkout until it is merged)` while
the work is still only on the run branch. The pre-merge and merge commits are
recorded in `state.json`, so `gcae undo <repo> <run-id>` resets your branch and refuses if it is
dirty or HEAD moved; `gcae merge <repo> <run-id>` performs the same merge later for any completed
run.

## Examples

```bash
gcae run ~/src/project "Add a --dry-run flag to the importer" \
  --criterion "command succeeds: pytest -q" --config ~/.config/gcae/config.toml

gcae run ~/src/project "Fix the parser" --headless > state.json
gcae resume ~/src/project 3cdf087083c2
gcae list && gcae inspect 3cdf087083c2
gcae merge ~/src/project 3cdf087083c2
gcae undo  ~/src/project 3cdf087083c2
```

A run that cannot make progress ends in `waiting_for_user` and the summary prints the question
plus the exact command to answer it (`gcae resume <repo> <run-id>`).

A headless run is observable while it works: the same events are appended to
`runs/<run-id>/events.jsonl`, so `tail -f` shows `provider_waiting` heartbeats and
`provider_progress` counts while a slow model deliberates.

Exit codes: `0` only when a run finished (`status == "complete"`); `1` for a handled error
(bad config, refused repository, refused merge) and for a run that ended `failed`, `stopped`
or `waiting_for_user`, so scripted callers can rely on the exit status; `2` argparse usage
errors.
