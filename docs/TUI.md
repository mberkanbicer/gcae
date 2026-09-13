# TUI

`gcae run` and `gcae resume` open an interactive Textual dashboard when stdout and stdin are a
TTY. `--headless` forces the non-interactive path, `--tui` forces the dashboard. Both modes share
the same engine; `src/gcae/tui/` imports the engine, never the other way round.

## Starting without a task

`gcae run <repository>` (no request) opens the TUI directly with a request modal:

```
Describe the task (Enter starts the run, Esc cancels)
> Add a --dry-run flag to the importer
```

On submit the runtime plans the task in the worker thread and the agent starts. Success criteria
are derived from the typed request by the configured planner (`[planner] kind = "auto"` uses the
model for HTTP providers; the planner is required to produce at least one checkable criterion).
Extra criteria can still be supplied with `--criterion` and are merged, never overwritten.

## Architecture

- The runtime runs in a Textual worker thread (`run_worker(..., thread=True)`).
- The runtime emits typed `Event`s to in-process subscribers; the JSONL history and the live TUI
  stream use the same model.
- The app subscribes from the UI thread with `call_from_thread`; a subscriber failure is logged
  and never stops a run.
- `RuntimeControl` is the thread-safe channel in the other direction: pause, resume, stop, and a
  queue of user instructions drained by the runtime at iteration boundaries (including while
  paused).
- Panels are refreshed from runtime state every 0.4 s and immediately on events.

## Panels

| Panel | Contents |
| --- | --- |
| Run | project, run id, status, phase, iteration, branch, worktree, elapsed time |
| Objective | original objective, current semantic goal, latest user instruction |
| Plan | every step with status `pending` / `active` / `completed` / `failed` / `skipped` |
| Git | accepted commit, accepted steps, candidate file count and names |
| Action | current action/tool, arguments, expected result, duration, artifact |
| Validation | deterministic result, diff check, command count, changed files, warnings |
| Memory | per-kind record counts for the current run |
| Context | configured token budget, estimated usage, pinned and omitted record counts |
| Model | provider, active model, controller role, latest evaluation decision |
| Log | timestamped event stream, scrollable, toggleable with `l` |

Rollback, replan, user override and completion are explicit log entries so trajectory changes are
visible.

## Keys

```
q  quit (stops a running agent safely)
p  pause before the next model or tool action
r  resume
s  stop the run and persist state
d  inspect the current candidate diff (scrollable modal)
i  inject a user instruction / override
l  toggle the event log
?  help
Esc closes modals
```

## Semantics

- **Pause** prevents the next model or tool action. A running subprocess is not killed; state stays
  consistent.
- **Stop** sets a flag checked at every iteration boundary. The runtime persists state with status
  `stopped` and leaves accepted checkpoints intact. A stopped run is resumable.
- **Quit** stops a running agent, then exits the app.
- **Override** queues a new instruction; the runtime stores it as immutable memory, discards
  speculative work, and replans. Accepted commits are never modified by an override.
- **Diff** shows `git diff` of the isolated worktree (the speculative candidate).

## Testing

`tests/test_tui.py` uses Textual's `run_test` pilot: dashboard rendering and completion, pause /
resume / stop keys, instruction modal submission, diff modal, and event-driven panel updates.
