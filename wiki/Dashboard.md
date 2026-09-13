# Dashboard

`gcae run` and `gcae resume` open the dashboard automatically on a terminal. `--headless` forces the
plain path, `--tui` forces the dashboard. Both use the same engine.

```
GCAE │ RUNNING │ parser-project │ 7c21a9 │ openrouter · qwen3-14b · ACT │ 12m43s
───────────────────────────────────────────────────────────────────────────────────────
OBJECTIVE  fix quoted records across chunk boundaries   ACTIVE       pytest -q tests/
PLAN       3/5 steps  ✓ inspect  ✓ reproduce  ● fix     CHECKPOINT   trusted a31fc42
NOW        carry quote state across the boundary        VALIDATION   ✓ diff-check  … pytest
CTX ██████░░░░ 9.8k/32k   MEM 18 facts · 6 decisions   ITER 12
EVENTS  20:31:04 ✓ checkpoint a31fc42 · 20:30:57 ↻ replan · 20:29:29 ↩ rollback
[p] Pause  [s] Stop  [d] Diff  [l] Logs  [m] Memory  [c] Context  [i] Instruct  [?] Help  [q] Quit
```

## Sections

| Section | Answers |
| --- | --- |
| status bar | which run, which state, which model and role, how long |
| OBJECTIVE + NOW | the original request and the goal being worked on right now |
| PLAN | the roadmap, with the current step highlighted and a `n/total` counter |
| ACTIVE | the tool or model call in flight, its arguments, expected result and elapsed time |
| CHECKPOINT | trusted commit, candidate CLEAN/DIRTY, per-file `+N -M`, rollback emphasis |
| VALIDATION | every check with exit code and duration, verification criteria and the last decision |
| metrics strip | context budget usage (estimated), memory counts, iteration |
| EVENTS | curated events: checkpoints, accepts, rollbacks, replans, validation, failures, questions |
| footer | only the shortcuts that are valid right now |

## Keys

| Key | Action |
| --- | --- |
| `q` | quit (stops a running agent safely) |
| `p` / `r` | pause / resume |
| `s` | stop the run (asks for confirmation) |
| `i` | inject an instruction — and revive a run that is waiting or stopped |
| `d` | candidate diff: file list plus rendered diff per file |
| `l` | full event log, `f` cycles filters |
| `m` | memory inspector with provenance |
| `c` | context inspector: exactly what the model receives |
| `e` / `t` | latest evaluation and evidence / full plan detail |
| `M` | merge accepted work when the run finished without merging |
| `Enter` | open the focused section's detail view |
| `Tab` `j` `k` | move focus between sections |
| `?` | help; `Esc` closes any dialog |

## Responsive behaviour

| Width | Behaviour |
| --- | --- |
| ≥ 140 | two columns, 12–16 event rows, full status bar |
| 100–139 | two columns, metrics include memory and iteration |
| 90–99 | metrics strip hidden, timeline 4–8 rows |
| < 90 | one stacked column in priority order (objective → plan → active → checkpoint → validation), the main region scrolls, timeline fixed at 4 rows |

Status, rules and footer stay pinned at every size; short terminals (< 40 rows) reduce the plan and
validation row budgets, and below 20 rows the timeline hides.

## Intervention semantics

- **Pause** prevents the next model or tool action; nothing is discarded and the status bar shows
  `PAUSED`.
- **Stop** ends the run at an iteration boundary, persists state, and leaves accepted checkpoints
  intact; the run stays resumable.
- **Instruction** is stored as immutable memory, discards speculative work and replans. If no loop is
  running (the run is waiting, stopped or failed) the dashboard applies the instruction and continues
  the same run instead of queueing it into nothing.
- **A stalled run asks.** When an approach is exhausted the status becomes `WAITING`, the question
  appears in the metrics strip, and `i` answers it.
