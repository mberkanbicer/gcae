# CLI Reference

```text
gcae run     <repository> [request] [--criterion TEXT]... [--constraint TEXT]...
             [--merge|--no-merge] [--no-auto-bootstrap] [--tui|--headless]
             [--config PATH] [--runtime-dir PATH]
gcae resume  <repository> <run-id> [--tui|--headless]
gcae list    [--config PATH] [--runtime-dir PATH]
gcae inspect <run-id> [--json]
gcae merge   <repository> <run-id>
gcae undo    <repository> <run-id>
```

## `gcae run`

Starts a new run. The request is optional on a terminal: without it the dashboard asks for the task
and the planner derives the success criteria. In headless mode a request is required.

| Flag | Effect |
| --- | --- |
| `--criterion TEXT` | a checkable success criterion; merged with planner-inferred ones |
| `--constraint TEXT` | a hard constraint the agent must respect |
| `--merge` / `--no-merge` | force or disable the merge of the verified branch |
| `--no-auto-bootstrap` | refuse to create the base commit instead of creating it |
| `--tui` / `--headless` | force the dashboard or the non-interactive path |
| `--config PATH` | TOML configuration file |
| `--runtime-dir PATH` | state directory (default `${XDG_STATE_HOME:-~/.local/state}/gcae`) |

## `gcae resume`

Continues a run from its persisted state: stopped, failed, or `waiting_for_user`. Speculative work
is rolled back to the last accepted checkpoint first, so a resume never continues from a half-applied
candidate. A worktree that was deleted is recreated from the run branch.

## `gcae list`

```
run id         status             steps  updated              objective
d1ae6b3af14d   complete               1  2026-09-13T22:08:13  fix quoted records
```

## `gcae inspect`

Objective, plan, per-criterion verification, merge state and pending question. `--json` prints the
persisted state verbatim, which is the ground truth for anything the dashboard shows.

## `gcae merge` / `gcae undo`

`merge` applies the same guards as automatic merging: the run must be complete (or have accepted
checkpoints), not already merged, and its branch must still point at the accepted commit; the
repository must be clean. Conflicts are handed to the agent, which resolves them in the run worktree
and re-verifies before the merge is retried.

`undo` resets the source branch to the recorded pre-merge commit and refuses when the repository is
dirty or HEAD moved after the merge.

## Exit codes

| Code | Meaning |
| --- | --- |
| `0` | the run completed **and** its work reached your checkout |
| `1` | handled error, a run that did not complete, or a merge that did not happen |
| `2` | argparse usage error |

## Reading a summary

```console
run d1ae6b3af14d: complete
accepted steps: 1, commit: d176e5f2effb87af4108bafb4ea0b5d699fecc59
verification: 1/1 criteria passed
worktree: ~/.local/state/gcae/worktrees/d1ae6b3af14d
branch: gcae/d1ae6b3af14d merged into main (undo: gcae undo ~/csv-parser d1ae6b3af14d)
files: parser.py
documents: ~/csv-parser (in your working tree now)
```

| Line | Meaning |
| --- | --- |
| `accepted steps` / `commit` | how many steps passed evaluation, and the trusted commit |
| `verification` | how many success criteria passed on the final tree |
| `worktree` | where the agent worked |
| `branch` | merge state plus the exact undo command |
| `files` | what the run produced |
| `documents` | which folder holds it right now |
| `question` | present only when the run is `waiting_for_user`, with the command to answer |
