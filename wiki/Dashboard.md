# Dashboard

```
gcae run /path/to/repo          # the dashboard opens when stdin/stderr is a terminal
gcae run /path/to/repo --tui    # force it
```

The dashboard is a live view of the run: it subscribes to the same events the log records, so what
you see is what happens. The engine itself never imports Textual — the runtime works headlessly, and
the TUI is one front end for it.

## Sections

| Panel | Contents |
| --- | --- |
| Status bar | app, run state, project, run id, active model and role, elapsed time, `ROLLBACK` / `DEGRADED` flags |
| Objective | the request, constraints and current goal |
| Plan | steps with status; the active step is expanded |
| Activity | what is happening now: tool, provider stream (characters, reasoning characters, preview), waiting state |
| Checkpoint | accepted commit, branch, merge record, undo hint |
| Validation | commands and their results, scope evidence, evidence counts (`N supporting · M contradicting · K records`) |
| Metrics | iterations, accepted steps, rollbacks, replans, context usage |
| Timeline | the run's story: plan, steps, validation, evaluation, trajectory verdicts, rollback, recovery, failover, merge |
| Banner | completion, stop or failure summary with the reason and the next step |
| Footer | keys valid in the current state |

## Failure visibility

The dashboard and the CLI tell the same story. The timeline carries:

| Event | Shown as |
| --- | --- |
| `trajectory_step_completed` | `trajectory accepted` / `candidate rejected` / `repair` / `trajectory replanned` / `trajectory blocked` |
| `evidence_recorded` (contradicting) | `evidence contradicts · <claim>` |
| `model_failover` | `controller failed over to <model>` |
| `model_escalated` | escalation to the stronger model |
| `runtime_degraded` | `degraded · <component> · <error>` |
| `success_criteria_adopted` | criteria adopted from the self-diagnosis |
| `rollback_failed` | a rollback that could not complete |
| `repeated_failure` | the same failure N times |
| `conflict_detected` / `resolved` / `unresolved` | merge conflict handling |
| `merge_completed` | merged into the target branch with the commit |
| `recovery_started` / `completed` / `failed` | the self-diagnosis and its correction |

A degraded run keeps a `DEGRADED` flag in the status bar naming the lost subsystem, because a
degradation is a condition rather than a moment.

## Keys

| Key | Action | Key | Action |
| --- | --- | --- | --- |
| `p` / `r` | pause / resume | `d` | candidate diff, per file |
| `s` | stop the run | `l` | event log |
| `i` | send an instruction, or start a new task | `m` | memory |
| `M` | merge accepted work (when done) | `c` | the exact context sent to the model |
| `t` | full plan | `e` | evaluation history |
| `enter` | open the focused panel | `?` | help |
| `tab`, `j` / `k` | move between panels | `q` | quit |

## Responsive behaviour

Panels reflow instead of truncating: wide terminals show the full set side by side, narrower ones
stack (context and logs collapse first, status and timeline always remain), and long objectives and
active steps wrap to a bounded number of rows, so a verbose step cannot push the timeline off screen.

## Intervention semantics

| Action | Effect |
| --- | --- |
| pause | no new model call or tool runs; state is untouched |
| resume | continues from the current checkpoint |
| stop | ends the loop, keeps every accepted commit, delivers the branch |
| instruction | immutable memory; speculative work is rolled back, the plan is revised, a waiting run is re-armed |
| merge | merges the accepted branch, the same operation as `gcae merge` |

Quitting the dashboard does not undo anything: the run's branch, state and memory stay exactly where
they were, and `gcae resume` continues them.
