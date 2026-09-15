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
│ OBJECTIVE  (request + NOW)  │ ACTIVE     (Goal · Doing · Last result · Next)          │
│ PLAN       (roadmap)        │ HEALTH      (guardian state, compact)                  │
│                             │ CHECKPOINT  (TRUSTED · CANDIDATE · file scope)          │
│                             │ VALIDATION  (checks · criteria · failures first)        │
│                             │ EVALUATION  (the latest decision and its reason)        │
├─────────────────────────────┴─────────────────────────────────────────────────────────┤
│ completion / failure banner (only when a run ends)                                    │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ CTX ███░░ 9.8k/32k 31% est   MEM 6 facts · 2 decisions   FAIL 1   ITER 12             │
├───────────────────────────────────────────────────────────────────────────────────────┤
│ EVENTS  (semantic only: checkpoints, accept/rollback/replan, validation, failures, user)│
├───────────────────────────────────────────────────────────────────────────────────────┤
│ [p] Pause  [s] Stop  [d] Diff  [l] Logs  [m] Memory  [c] Context  [i] Instruct  [?] …  │
└───────────────────────────────────────────────────────────────────────────────────────┘
```

Differences from a plain log viewer:

- **No decorative panels.** Sections use one dim uppercase title row and aligned label columns;
  borders are reserved for dialogs.
- **State, not telemetry.** The main screen answers "what is being accomplished, with what, and
  how far did it get". Model streaming — first tokens, character counts, partial generations,
  heartbeats — is *debug* detail: it never enters the timeline and never reaches the ACTIVE panel.
  The log screen (`l`) keeps every raw event, including the streamed preview.
- **The screen does not move.** Every panel is either *fixed* (its content changes several times a
  step: OBJECTIVE 5 rows, ACTIVE 7, CHECKPOINT 6, VALIDATION 5) or flexible (`1fr`, it absorbs the
  slack: PLAN and EVALUATION). The timeline is fixed per breakpoint (4–11 rows). No panel is content
  sized, so a model that starts streaming — or a candidate that gains a file, or a check that
  finishes — changes what is *inside* a box and never where the box is. Content that does not fit is
  windowed (plan steps, check rows, timeline entries) with a count of what is hidden.
  Verified by `tools/tui_demo.py --frames` over a live run: 12 consecutive frames across a streaming
  call, a candidate change, a validation and a completion, with every panel's geometry identical,
  and by `test_panel_boxes_never_move_while_a_model_streams` (which fails if any fixed box is turned
  back into a content-sized one).
- **Curated events.** Routine tool successes and model telemetry are not timeline entries; the full
  stream is one keystroke away (`l`). Tool failures, rollbacks, replans, checkpoints, validation
  results, user instructions and terminal states always appear.
- **Three detail levels, never mixed.** *Level 1* is the main timeline: one categorized line per
  meaningful action or outcome (`INSPECT Reviewed 3 relevant files`, `ROLLBACK Restored c00a1ba`),
  with consecutive reads grouped and outcomes resolved in place. *Level 2* is the dedicated detail
  screens — `Enter` on the focused section, plus `d` diff, `e` evaluation, `t` trajectory, `h`
  health, `m` memory, `c` context — command output, evidence records, evaluator reasons.
  *Level 3* is raw telemetry: provider chunks, character counts, retry messages, stack traces —
  Logs only, and never the default view. "Detailed" never means a token stream.
- **A slow model is never a frozen screen.** Every model call is bracketed by `provider_started` /
  `provider_finished` events; a silent call emits a `provider_waiting` heartbeat every 10 s. The
  ACTIVE panel shows one calm row (`controller · generating · 3.4s`) plus the model name, and the
  elapsed time keeps moving without the layout moving with it.
- **A stalled run asks, it does not die.** When an approach is exhausted the status bar shows
  `WAITING`, the request/question appears in the metrics strip, and `i` (or `r`) applies your
  instruction and continues the same run with its accepted checkpoints intact.
- **Conflicts are handled too.** A conflicting merge appears in the timeline, the agent resolves
  the markers in its own worktree, the run re-verifies and the merge is retried — or the branch is
  left untouched and the banner says why.
- **The loop owns git.** Base commit, identity, branch, worktree, merge and cleanup happen
  without the user running git; the dashboard reports each of them in the timeline.
- **You always know where the files are.** The end-of-run banner lists the produced documents and
  the folder that holds them right now — your repository once merged, otherwise the worktree path
  with a note that nothing has reached your checkout yet.
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
| ≥ 140 | two columns, full status bar (project, run id, provider, model, role, elapsed), 10 event rows |
| 100–139 | two columns, model and provider shortened, elapsed shown, 6–8 event rows |
| 80–99 | single stacked column in priority order (objective/NOW → plan → active → checkpoint → validation → evaluation), the main region scrolls, 3–4 event rows |
| < 80 | same stacked order, metrics strip reduced to context and memory, 3 event rows |

Height decides how much history fits: ≥ 40 rows gives long panels, 30–39 medium, 18–29 short, and
below 18 rows the timeline is hidden while the status bar, rules and shortcut footer stay pinned.
Every panel keeps a floor (NOW, ACTIVE, CHECKPOINT and PLAN always render their headline), so the
dashboard never becomes a scrolled list of empty boxes.

Panel row budgets follow the space the layout actually gives them (`Panel.row_budget`), which is why
a taller terminal shows more plan steps and more checks instead of a blank band at the bottom of a
box.

## Interactive processes

When a command is running, ACTIVE names its execution mode (`BATCH`, `SCRIPTED INPUT · 3 answers
queued`, `INTERACTIVE PTY · may ask for input`). When a live process asks for something the runtime
cannot know, the panel becomes a request instead of a countdown:

```
INPUT REQUIRED
process     python deploy.py
prompt      the process is waiting for: Enter deploy token:
state       WAITING FOR USER · press i to send the answer
```

`i` opens the process-input dialog, the answer goes to the process stdin (or is recorded as an
instruction when the process did not survive a restart), and a sensitive prompt is never written to
the event log. The timeline carries the adaptive story: `obstacle · interactive_input_required · …`,
`interactive input detected · <prompt>`, `strategy ineffective after N attempts`, `execution evidence
required`, and `blocked · …` when no safe autonomous path remains.

## Trajectory and evidence

The dashboard is a live trajectory inspector. Each semantic attempt appears on the timeline with
its verdict — `trajectory accepted · <goal>`, `candidate rejected · <reason>`,
`repair · <reason>`, `trajectory replanned · <reason>`, `trajectory blocked · <reason>` — and the
validation panel carries the running evidence counts (`3 supporting · 1 contradicting · 9 records`).
Contradictory evidence gets a timeline line of its own (`! evidence contradicts · <claim>`);
routine supporting evidence stays in the logs, where the streaming telemetry also lives.

## Plan history

The PLAN panel keeps verified completed steps visible and muted under a `vN · M/K verified`
header — history never disappears on replan. Invalidated steps show their reason briefly
(`× setup · invalidated: PTY evidence …`); replaced steps name their successor.
Replan timeline lines carry the scope (`preserved 3 · replaced 1 · added 2`). `[t]` opens the
trajectory view: plan steps plus every attempt with its verdict, knowledge gained and evidence
count. See `docs/PLAN_HISTORY.md`.

## Trusted versus candidate

The CHECKPOINT panel is the run's identity: it shows the trusted commit (a verified tree) and the
speculative candidate (the worktree right now), never a raw porcelain dump.

```
TRUSTED    1f383c6  Implement simulation model
CANDIDATE  DIRTY · 2 files · +86 -4
           M src/simulation.py   +24 -5
           A tests/test_simulation.py   +3 -0
           + 7 more files · press d for the diff
```

When there is nothing speculative it says `CLEAN · no speculative changes`; after a rejection it
adds `restored after rejection · <commit> is trusted`. The words carry the meaning, so the panel
still reads correctly without colour.

## Validation and evaluation

VALIDATION is a structured check list — `✓` pass, `×` fail, `…` running, `–` not applicable — and
it never dumps command output. A failing check carries its shortest honest detail (`exit 1`,
`Expected: 120`). Rows are ordered failures first, then the verification gate, then passing checks,
so a short panel cannot hide the check that failed.

EVALUATION is a separate section showing the latest decision word (`ACCEPTED`, `ROLLBACK`, `REPLAN`,
`CONTINUE`, `FINISH CANDIDATE`) and the evaluator's stored reason — never chain-of-thought, never
raw JSON. The full structured evaluation, including promoted memories and the next goal, is on `e`.

## Failure visibility

The dashboard and the CLI must tell the same story. The timeline carries the failures that used to
appear only in the raw log panel: `model_failover`, `model_escalated`, `runtime_degraded`,
`success_criteria_adopted`, `rollback_failed`, `repeated_failure`, and the merge events
(`conflict_detected`, `conflict_resolved`, `conflict_unresolved`, `merge_completed`). A degraded run
keeps a `DEGRADED` flag in the status bar naming the last lost subsystem, because a degradation is a
condition rather than a moment, and only a state-file failure (which stops the run) is not shown
there — it ends the run with its reason in the banner.

## Screens

| Key | Screen | Contents |
| --- | --- | --- |
| `d` | Diff | file list (`M`/`A`/`D`/`R` + `+N -M`) on the left, colourised unified diff on the right, `j/k`/`↑`/`↓` to switch, `Esc` to close. New files are diffed with `git diff --no-index`; clean trees say so explicitly |
| `l` | Logs | the full event stream with timestamps; opens on the **semantic** filter (decisions, failures, recovery, checkpoints — no telemetry); `f` cycles filters (semantic / all / model / guardian / tools / context / git / validation / evaluation / errors); follows the tail until you scroll up |
| `m` | Memory | stored records grouped by kind (`USER_INSTRUCTION`, `FACT`, `DECISION`, `FAILURE`, …) with step, commit, timestamp and source provenance |
| `c` | Context | the request payload actually sent to the model: section list with size share, plus the selected section's content |
| `e` | Evaluation | latest decision and reason, next goal, promoted memories, verification criteria with evidence, validation command output tails |
| `t` | Trajectory | plan steps plus every attempt with verdict, knowledge and evidence; completed history stays visible |
| `h` | Health | the guardian's view per subsystem — runtime, model, worktree, memory, event store, active process, last recovery — built from live probes, never painted green |
| `Enter` | Inspect | opens the detail screen for the focused section (plan, checkpoint→diff, validation/active→evaluation, objective→context, events→event detail: the timeline's own events with command, outcome and evidence, `j/k` to step) |

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

`tools/tui_demo.py` replays a realistic trajectory through a real runtime (real git, real
validation, real rollback) against a temporary repository — including the **streaming telemetry a
live provider emits**, which is what keeps the "telemetry must not reach the main screen" rule
honest:

```bash
python tools/tui_demo.py                                     # interactive dashboard
python tools/tui_demo.py --plain --size 160x45               # final frame as text
python tools/tui_demo.py --plain --size 90x30 --capture 13   # narrow frame, mid-run

# layout stability: consecutive frames + panel geometry while the model streams
python tools/tui_demo.py --plain --size 160x45 --capture 3.2 --frames 12 --interval 0.4
```

`--frames` prints the geometry of every panel (x, y, width, height, body rows) next to each frame and
scans the timeline and ACTIVE bodies for telemetry markers, so a reflow or a leaked counter is
visible as a diff instead of something to squint at.

`tools/tui_demo.py` replays a realistic run against a temporary repository using real git
operations, real validation commands and a real rollback and checkpoint, so the interface can be
inspected without spending model tokens:

```bash
.venv/bin/python tools/tui_demo.py                          # interactive
.venv/bin/python tools/tui_demo.py --plain --size 150x46     # final frame as text
.venv/bin/python tools/tui_demo.py --plain --size 80x28 --capture 12   # mid-run frame
```

## Testing

`tests/test_tui.py` covers rendering for the empty, running, dirty-candidate, validating,
rollback, replan, paused, complete and failed states; every event → section mapping; the reducer;
detail screens; key handling; and five terminal sizes (160×45 down to 60×18). Redesign-specific
coverage:

| Test | Contract |
| --- | --- |
| `test_streaming_telemetry_never_reaches_the_semantic_timeline` | provider events stay out of the timeline and remain in the logs |
| `test_the_active_panel_shows_state_not_streaming` | no character counts or generated text on the main screen |
| `test_the_active_panel_shows_one_calm_row_while_a_model_generates` | one state row while a model call streams |
| `test_a_dirty_candidate_is_announced_once_per_step` | `candidate_state` cannot flood the timeline |
| `test_the_checkpoint_panel_separates_trusted_from_candidate` | TRUSTED/CANDIDATE structure and file scope |
| `test_the_validation_panel_reports_structured_checks` | structured `✓`/`×` rows with failure detail, no evaluator decision |
| `test_the_evaluation_panel_shows_the_decision_and_reason` | decision word plus reason, empty state included |
| `test_each_run_state_renders_its_headline` | dirty/validation/rollback/accept/replan/finish all visible |
| `test_responsive_smoke[160×45 … 60×18]` | priority-1 panels render at every size, stacked below 100 columns |

`tests/test_control.py` and `tests/test_phase2.py` cover the runtime events and git data the
dashboard renders. Formatters have direct unit tests.
