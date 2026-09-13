# TUI

`gcae run` and `gcae resume` open an interactive Textual dashboard when stdout and stdin are a
TTY. `--headless` forces the non-interactive path, `--tui` forces the dashboard. Both modes share
the same engine; `src/gcae/tui/` imports the engine, never the other way round.

The dashboard answers, at a glance: what is the agent trying to do, where is it now, what is it
doing right now, has real progress been made, what changed in Git, what failed, did it roll back,
what did it learn, how much context is in use, which model is active, and how to intervene.

## Starting without a task

`gcae run <repository>` (no request) opens the dashboard with a request modal. On submit the
runtime plans the task in the worker thread; success criteria are derived from the request by the
configured planner (`[planner] kind = "auto"` uses the model for HTTP providers and must produce at
least one checkable criterion). `--criterion` values are merged, never overwritten.

If the run cannot start (dirty repository, no commits, missing git identity, planner failure) the
failure banner explains the reason and keeps the accepted checkpoint visible; `i` re-opens the task
prompt so the same session can be retried after the repository is fixed.

## Layout

```
┌ top status bar ───────────────────────────────────────────────────────────────────────┐
├─────────────────────────────┬─────────────────────────────────────────────────────────┤
│ OBJECTIVE  (original + NOW) │ ACTIVE      (goal · tool · state · expectation)         │
│ PLAN       (roadmap)        │ CHECKPOINT  (trusted · candidate · file scope)          │
│                             │ VALIDATION  (checks · criteria · last decision)         │
├─────────────────────────────┴─────────────────────────────────────────────────────────┤
│ completion / failure banner (only when a run ends)                                    │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ CTX ███░░ 9.8k/32k (est)   MEM 18 facts · 6 decisions   ITER 12                       │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ EVENTS  (curated: checkpoints, accept/rollback/replan, validation, failures, user)     │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ [p] Pause  [s] Stop  [d] Diff  [l] Logs  [m] Memory  [c] Context  [i] Instruct  [?] …  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Differences from a plain log viewer:

- **No decorative panels.** Sections use one dim uppercase title row and aligned label columns;
  borders are reserved for dialogs.
- **Slack becomes history.** Panels are content sized with caps, and the event timeline takes the
  remaining rows, so idle screens show more history instead of a blank band.
- **Curated events.** Routine tool successes are not timeline entries; the full stream is one
  keystroke away (`l`). Tool failures, rollbacks, replans, checkpoints, validation results, user
  instructions and terminal states always appear.
- **Failed runs are recoverable.** A run that accepted checkpoints and then failed says so in the
  banner (`RUN FAILED` plus `N accepted step(s) are on branch …`) and offers `M` to merge the
  accepted work; the banner then marks it `accepted work, final verification did not pass`.
- **Done means visible.** Completing a run merges the verified branch into your current branch in a
  worker thread; the banner reports the target branch, the merge commit and the `gcae undo`
  command. If the merge cannot proceed (dirty checkout, moved branch, no changes) the timeline says
  why and the branch stays for `M` or `gcae merge`.
- **Failures are diagnosable.** A failed content criterion shows what was expected and what is
  actually there, and a run that ends without completing exits non-zero.
- **Self-repair is visible.** Git preconditions GCAE fixes appear at the top of the timeline
  (`+ base commit created · 3 files · a31fc42`) along with the fallback commit identity
  (`! git user.name/user.email are not configured; commits use GCAE <gcae@localhost>`).
- **Real data only.** Candidate file counts and `+N -M` come from `git diff --numstat` plus a
  bounded line count for new files; token figures are labelled `(est)`; nothing is synthesized.

## Responsive behaviour

| Width | Behaviour |
| --- | --- |
| ≥ 140 | two columns, full status bar (project, run, provider, model, role, elapsed), 12–16 event rows |
| 100–139 | two columns, metrics strip shows memory and iteration, 8–12 event rows |
| 90–99 | two columns, metrics strip hidden, timeline 4–8 rows |
| < 90 | single stacked column in priority order (objective/NOW → plan → active → checkpoint → validation), the main region scrolls, timeline fixed at 4 rows, status bar shortened |

Short terminals (< 40 rows) reduce the plan and validation row budgets; below 20 rows the timeline
is hidden. The status bar, rules and shortcut footer stay pinned at every size.

## Screens

| Key | Screen | Contents |
| --- | --- | --- |
| `d` | Diff | file list (`M`/`A`/`D`/`R` + `+N -M`) on the left, colourised unified diff on the right, `j/k`/`↑`/`↓` to switch, `Esc` to close. New files are diffed with `git diff --no-index`; clean trees say so explicitly |
| `l` | Logs | the full event stream with timestamps; `f` cycles filters (all / model / tools / context / git / validation / evaluation / errors); follows the tail until you scroll up |
| `m` | Memory | stored records grouped by kind (`USER_INSTRUCTION`, `FACT`, `DECISION`, `FAILURE`, …) with step, commit, timestamp and source provenance |
| `c` | Context | the request payload actually sent to the model: section list with size share, plus the selected section's content |
| `e` | Evaluation | latest decision and reason, next goal, promoted memories, verification criteria with evidence, validation command output tails |
| `t` | Plan | every step with rationale, expected result, intended scope and validation requirements |
| `Enter` | Inspect | opens the detail screen for the focused section (plan, checkpoint→diff, validation/active→evaluation, objective→context, events→logs) |

## Keys

```
q            quit (stops a running agent safely first)
p / r        pause / resume
s            stop the run (confirmation dialog; Enter stops, Esc cancels)
i            inject a user instruction, or re-open the task prompt after a failure
d l m c e t  diff, logs, memory, context, evaluation, plan detail
Enter        open the focused section's detail screen
Tab j k      move focus between sections (Shift+Tab backwards)
?            help · Esc closes any dialog or screen
```

## Semantics

- **Pause** prevents the next model or tool action; the candidate and accepted checkpoint are left
  untouched and the status bar shows `PAUSED` with only `[r] Resume` in the footer.
- **Stop** asks for confirmation, sets a flag checked at every iteration boundary, persists state
  with status `stopped` and leaves accepted checkpoints intact. A stopped run is resumable.
- **Instruction** is queued through `RuntimeControl`, stored as immutable memory, discards
  speculative work and replans; the timeline acknowledges the queued instruction immediately.
- **Rollback** is never silent: the checkpoint panel and status bar highlight it for ~20 s with the
  discarded file count and the restore commit, and the timeline keeps a permanent entry.
- **Quit** stops a running agent before exiting.

## Architecture

```
src/gcae/tui/
    app.py         composition, bindings, worker threads, event routing
    state.py       presentation reducer (events in, panel data out); no runtime truth
    formatters.py  pure rendering helpers (badges, plan markers, durations, diff colours)
    widgets.py     one widget per information zone
    screens.py     diff, logs, memory, context, plan and evaluation viewers
    modals.py      instruction/request input, stop confirmation, help
    styles.tcss    semantic layout + focus styles
```

- The runtime runs in a Textual worker thread; the UI thread never blocks on git, SQLite or the
  provider. Git status and diffs are collected in a second worker.
- Runtime events are folded by `UiState.apply`, which returns the set of sections that changed, so
  updates are targeted instead of a full repaint.
- A 0.5 s tick only refreshes presentation derived from wall-clock time (elapsed counters,
  rollback emphasis expiry) and the run badge.
- Panels read `runtime.state` for authoritative values and never own a second copy of it.
- High-volume events (`context_built`, `candidate_state`, `memory_updated`, `phase:*`) update
  metrics without touching the timeline.

## Developer demo

`tools/tui_demo.py` replays a realistic run against a temporary repository using real git
operations, real validation commands and a real rollback and checkpoint, so the interface can be
inspected without spending model tokens:

```bash
.venv/bin/python tools/tui_demo.py                          # interactive
.venv/bin/python tools/tui_demo.py --plain --size 150x46     # final frame as text
.venv/bin/python tools/tui_demo.py --plain --size 80x28 --capture 12   # mid-run frame
```

## Testing

`tests/test_tui.py` covers rendering for empty/running/paused/completed/failed states, every event
→ section mapping, the reducer, detail screens, key handling and five terminal sizes (160×45 down
to 60×18). `tests/test_control.py` and `tests/test_phase2.py` cover the runtime events and git data
the dashboard renders. Formatters have direct unit tests.
