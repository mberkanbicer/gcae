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

## Runs

- `list` prints known runs (id, status, accepted steps, update time, objective), newest first.
- `inspect` prints objective, plan, verification per criterion, merge state and pending question;
  `--json` emits the persisted state verbatim.

## Merge / undo

After a successful run the CLI asks whether to merge the verified branch. `--merge` answers yes
without asking, `--no-merge` disables the question. `gcae merge` performs the same merge later for
any completed run whose branch still points at the verified commit. Every merge records the
pre-merge commit; `gcae undo` resets the source branch back and refuses if the repository is dirty
or HEAD moved. GCAE never merges on its own.

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

Exit codes: `0` only when a run finished (`status == "complete"`); `1` for a handled error
(bad config, refused repository, refused merge) and for a run that ended `failed`, `stopped`
or `waiting_for_user`, so scripted callers can rely on the exit status; `2` argparse usage
errors.
