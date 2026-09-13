# CLI

```
gcae run <repository> <request> [--config PATH] [--runtime-dir PATH]
         [--constraint TEXT]... [--criterion TEXT]... [--merge|--no-merge] [--tui|--headless]
gcae resume <repository> <run-id> [--config PATH] [--runtime-dir PATH] [--tui|--headless]
gcae list [--config PATH] [--runtime-dir PATH]
gcae inspect <run-id> [--config PATH] [--runtime-dir PATH] [--json]
gcae merge <repository> <run-id> [--config PATH] [--runtime-dir PATH]
gcae undo <repository> <run-id> [--config PATH] [--runtime-dir PATH]
```

`run` and `resume` open the TUI on a terminal; non-interactive environments (or `--headless`) get
the event log on stderr plus the final `AgentState` JSON on stdout. The summary line reports
status, accepted steps, verification, worktree and branch/merge state.

## Criteria

Criteria are verified deterministically; unsupported criteria fail closed.

- `file exists: path/to/file`
- `file contains: path/to/file :: expected text`
- `command succeeds: pytest -q`

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

Exit codes: `0` success, `1` handled error (bad config, dirty repository, refused merge, failed
run), `2` argparse usage errors.
