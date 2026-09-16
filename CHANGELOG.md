# Changelog

All notable changes to GCAE are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [0.7.0] — 2026-09-16

### Added

- **Google Gemini provider (`kind = "google"`).** Reuses the OpenAI-compatible provider
  against Google's `/v1beta/openai/` endpoint (chosen by default when `base_url` is left
  unset); the native `/v1beta/interactions` URL is rejected with a guided error because it
  is a different protocol. `model` is required and the key defaults to `GEMINI_API_KEY`.
  Verified live end to end (fib.py run completed, 2/2 criteria, merged). Measured
  free-tier behaviour is documented in `docs/PROVIDERS.md`: `max_tokens` above 32768 gets
  a deterministic 503, `reasoning_effort` gets an instant 503 on 3.x models, the quota is
  ~20 requests/day/model at ~10 RPM.
- **Request pacing (`min_request_interval`).** A process-wide pace gate shared by every
  role provider instance, enforced before each HTTP attempt so planner/controller/evaluator
  fan-out cannot burst past per-minute rate limits and starve the run in a 429 storm.
  Provider 429 `Retry-After` is still honoured on top. Live-verified against Gemini
  (1×429, 0×503 on the completing run).
- **CLI `--request`/`-p` and headless-by-default.** The task can be passed as a flag instead
  of the positional (both at once is a usage error), and a request given on the command
  line now runs headlessly instead of opening the TUI — `--tui` forces the dashboard with
  it pre-filled. Without a request, terminal behaviour is unchanged (interactive ask).
- **Tool runtime upgrades.** `search_text` gains `regex`/`include`/`context_lines`/
  `max_matches`; file writes are atomic (sibling temp + `os.replace`) with
  validate-all-before-write and rollback for the batch ops; `create_file` refuses to
  overwrite via `os.link`; `run_command` gains worktree-relative `cwd` and per-command
  `env`; `fetch_url` resolves DNS once and allows only public addresses (SSRF guard).
  Documented in `docs/TOOLS.md`.
- **Prune DB semantics pinned.** A test locks in that `gcae prune` deletes execution
  records only: the shared `memory.db` ledger (lessons + evidence rows) survives pruning
  by design — knowledge is cumulative, retrieval stays scoped by `run_id`/`source_repo`.

## [0.6.2] — 2026-09-16

### Added

- **Token usage accounting.** The provider keeps the last response's token counts
  (`prompt_tokens`, `completion_tokens`, `total_tokens`) on `last_usage`; the runtime
  copies them into the `provider_finished` event payload. Usage lives at event-detail
  level only — never on the main UI. Verified live against OpenRouter (fib.py baseline
  run completed end to end) and deterministic at file level for buffered + streamed
  responses.

### Fixed

- **Content refusals are named, not masked.** A `message.refusal` (e.g. content filter)
  now appears in the failure reason instead of being reported as generic empty content;
  retrying a refusal cannot help, so the repair budget is no longer spent silently.
- Honest scope note: the 429/5xx retry ladder, truncation escalation, stall detection
  and empty-content paths were re-verified against the live baseline and already had
  deterministic coverage — no changes needed there.

## [0.6.1] — 2026-09-16

### Fixed

- **Cross-step criterion impact (PH §43, the last planning deferral).** Invalidating a
  step now also moves verified criteria proven by *surviving* steps that depend on the
  invalidated one to `revalidation_required` — their proof rests on dead work; the
  dependent step itself stays completed. Deterministic dependency-graph rule, one
  implementation shared by all three invalidation paths (step reopen, replan patch,
  rollback reconciliation).
- **Latent evidence-linkage bug** (found while implementing the above): the unverify
  pass compared trajectory ids (`trajectory-step-2-1`) against bare plan ids (`step-2`)
  and never matched in real runs — only the fabricated test form matched. The mapping
  now resolves the attempt suffix and still accepts bare ids from older records.
- **Replan patches never unverified criteria** of the steps they invalidated — only
  step-reopen and rollback reconciliation did. Stale `verified` claims after a replan
  are gone.

### Docs

- `docs/PLAN_HISTORY.md`: the un-verification rule now states cross-step semantics and
  the trajectory-id linkage; `docs/IMPLEMENTATION_AUDIT.md`: the §43 deferral is closed
  with both latent bugs recorded honestly.

353 tests pass.

## [0.6.0] — 2026-09-16

### Added — resume robustness and plan-consistency recovery

- **Git-side resume reconciliation.** A crash between a checkpoint commit and the state
  write, or mid event-append, is reconciled *and explained*: an unrecorded descendant
  commit is discarded and named in a `resume_reconciled` event (subject included); a
  diverged branch is restored forward and reported; `EventLog.repair_tail()` truncates a
  torn trailing line (never mid-file damage); the stale `state.json.write` temp is
  removed. (W1, W6)
- **Plan↔git consistency repair on resume.** When execution sits behind the trusted
  checkpoint, the ancestry-based reconciliation invalidates exactly the steps whose
  checkpoints did not survive (dependents follow, knowledge stays) under the new
  `resume_reconciliation` reason category — a shared `ReplanReason` literal now types
  every plan revision. (W5, PH §21)
- **Criterion revalidation.** Verified criteria whose supporting evidence died with an
  invalidated step move to `revalidation_required`: never silently deleted, never falsely
  verified. Guardian plan-health treats them as covered (final verification re-checks),
  the replan prompt plans a cheap revalidation step, and passing final verification
  clears them. (W7, PH §16/§42)
- **Crash-safe merge lifecycle.** A `PendingMerge` marker persists before `git merge`;
  on any later attempt, an already-performed merge is backfilled from git ancestry —
  never merged twice, never left without an undo record, `gcae undo` keeps working. A
  crash between completion and the merge is closed by the existing resume path. (W2, W3)
- **Blocked-run resume contract.** A plain `resume` of a `blocked` or `waiting_for_user`
  run holds it — reason, hint and question survive the restart and `run()` does not work
  behind the block. Proceeding takes an instruction (which now also clears
  `blocked_reason`) or `gcae resume --force` (recorded as `resume_forced`). (W4)
- **Per-event detail screen.** `Enter` on the timeline opens the run's semantic events
  one at a time — command, outcome, evidence ids, severity, bounded payload — with `j/k`
  stepping newest-first. Raw telemetry stays in Logs. (R §37)
- **Resume after accepting the last step.** A crash between the acceptance persist and
  queueing the next step leaves `current_step_id` on a completed step — normal mid-loop
  state that plan-health used to reject as `current_step_missing`. Resume now advances
  the pointer or queues exactly one follow-up step (history untouched). Found by the
  SIGKILL test, verified deterministic at file level.
- `tests/test_resume_recovery.py` (15): every crash window at file level plus a **real
  SIGKILL** run killed after its first checkpoint and resumed to completion in a second
  process.

### Fixed

- `Guardian.plan_health` crashed on empty plan history (0.5.2 already noted; covered
  again by the direct invariant test).
- Earlier audit corrections recorded in `docs/IMPLEMENTATION_AUDIT.md`: typed replan
  reasons and the ACTIVE model row were already shipped — the audit greps were wrong.

### Tests

349 pass (15 in tests/test_resume_recovery.py incl. the real SIGKILL resume, plus the
event-detail TUI test). Docs updated: STATE_MACHINE (resume
transitions + crash-window table), CLI (`resume --force`, notices), FAILURE_RECOVERY
(crash windows), PLAN_HISTORY (typed reasons, revalidation), TUI (event detail),
AGENTS.md invariant 5.

## [0.5.2] — 2026-09-15

### Fixed

- **Doc sync (the §86 gap):** `FAILURE_RECOVERY.md` (Guardian's place in the ladder),
  `CONTEXT_RECONSTRUCTION.md` (retrieval tiers, duplicate gate, `context_warning`),
  `ARCHITECTURE.md` (Guardian-wrapped pipeline and the progress presentation layer) and
  `TUI.md` (three detail levels, semantic-first Logs default, `[h]` Health screen row,
  ACTIVE Goal/Doing/Last/Next) now describe shipped behavior instead of the pre-0.5.0 UI.
- **Guardian `plan_health` crashed on empty plan history** (`history[-1]` before the
  guard) — exposed by the new direct test; empty history is now valid.
- `context_warning` had no presentation mapping: it now renders as one deliberate SYSTEM
  line (`Retrieved memory 44% of context · 12 duplicates dropped`) and the runtime emits it
  once per run, so a persistently heavy memory share cannot flood the timeline.
- The §71 journal test now asserts the full required sequence, including
  `Validation passed · 1 checks` and `Checkpoint 7fa89c2` (10/10 lines).

### Added

- `tests/test_guardian.py::test_event_store_failure_is_detected_and_visible` (§78:
  events.jsonl unwritable → `runtime_degraded` + state degradations, run completes
  visibly degraded, never silently healthy) and
  `test_plan_health_reviews_every_invariant_directly` (duplicate IDs, version mismatch,
  dangling dependency, non-executable current step, missing checkpoint linkage, uncovered
  criterion — each classified and routed to `MARK_BLOCKED`).
- Deferred gaps recorded honestly in `docs/IMPLEMENTATION_AUDIT.md`: typed replan-reason
  categories, criterion revalidation model, per-event timeline expansion, in-panel model
  row.

### Tests

334 pass (+2 guardian, +1 progress).

## [0.5.1] — 2026-09-15

### Changed

- Wiki synced with 0.5.0: Concepts gains **Plan history** and **Runtime Guardian**
  sections; Dashboard panels and keys refreshed (single primary status, health panel,
  trajectory view, semantic-first log filter).
- Corrected the 0.5.0 changelog test count (331, not 340).

### Tests

331 pass, unchanged (documentation-only release).

## [0.5.0] — 2026-09-14

### Added

- **Stable plan history and partial replanning.** The plan is trajectory state with a
  locked verified prefix, the current step and an adaptive future suffix. Completed steps
  keep stable IDs, checkpoint linkage and locks across replans; replans are deterministic
  `ReplanPatch`es to the affected region (validated: base version, locked-step evidence,
  no duplicates, criteria coverage preserved); invalidation needs recorded reason plus
  ledger evidence and follows `depends_on`; rollback to an older checkpoint invalidates
  exactly the steps that no longer survive it (ancestry-checked); every revision bumps
  `plan_version` with a persisted `PlanVersion` record. (docs/PLAN_HISTORY.md)
- **Runtime Guardian** (`src/gcae/guardian.py`): deterministic in-process supervision —
  pre/during/post checks for every model call and tool operation, step-boundary integrity
  checks, heartbeat with soft/hard stall detection, bounded recovery budgets, stale-state
  repair, and plan-health review. Decides recoveries; the runtime executes them. No LLM,
  no second agent. (docs/RUNTIME_GUARDIAN.md)
- **Three presentation levels.** `ProgressEvent` (`src/gcae/progress.py`) drives the main
  timeline as an engineering journal (grouped reads, in-place outcomes, categories);
  Active shows Goal/Doing/Last result/Next with the execution phase; a compact Health
  panel plus `[h]` detail screen show one primary state; `[t]` is now the trajectory
  view; Logs defaults to the semantic filter with raw telemetry one key away.
  (docs/PROGRESS_MODEL.md)
- **Memory isolation tiers.** Retrieval is run-first, then same-project, then explicitly
  global-only, with duplicate gating and anomaly warnings; the context screen shows the
  tiers inline (`Memory[id|kind|tier]`).

### Fixed

- A blocked run no longer keeps executing behind the block: the loop halts on `blocked`.
- `current_step_id` advances on every replan/accept path, so plan-health checks see the
  real current step.
- An explicit contradiction never counts as support via claim matching.

### Tests

331 pass. New: `tests/test_plan_history.py` (§60–67: prefix preservation, earlier
invalidation with dependents, unrelated protection, replan-without-rollback,
rollback-without-replan, user override, stale rejection, locked rewrite, resume),
`tests/test_guardian.py` (failure checks, budgets, stalls, health priority, hang
termination, ladder recovery), `tests/test_progress.py` (incl. the §71
engineering-journal test), plus TUI story tests (Goal/Doing/Last/Next, health,
trajectory screen, replay, plan history).

## [0.4.1] — 2026-09-14

### Added

- **Retention TTL for `gcae prune`.** `--older-than DAYS` deletes runs older than the TTL
  even within `--keep`; `[runtime] run_retention_days` applies the same default without the
  flag. Live runs and merge-recorded runs are still kept; pruning removes run directories
  only and deliberately leaves the cumulative `memory.db` alone.
- **Legacy memory backfill.** Opening a run attributes pre-0.4.0 memory rows (empty
  `source_repo`) to their run's repository from the persisted run states, best-effort and
  never run-critical — scoped retrieval now sees old knowledge where the repo is known.
- **Explicit evidence recency.** The judge bundle is newest-first with `created_at` on every
  cited record (no scoring); a repaired run's fresh support outranks the stale contradiction
  it replaced, and contradiction-only verdicts cite newest first.

### Fixed

- An explicit contradiction no longer counts as support via claim matching: a record that
  says a criterion is false is contradicting-only, so pure contradiction fails fast without
  asking the judge, and PASS verdicts cite only what supports them.

### Tests

286 pass (+7): backfill store + runtime wiring, TTL prune / rejection / config fallback,
judge newest-first ordering and newest-first contradiction citation.

## [0.4.0] — 2026-09-14

### Added

- **TrajectoryStep is the primary execution entity.** Every semantic attempt is now a typed,
  persisted record (`state.json`, bounded) answering: what was the goal, what was expected,
  what evidence would prove it, which failure signals would disprove it, what happened, why
  was it accepted or rejected, and what was learned — with no chat history. Events
  `trajectory_step_started` / `trajectory_step_completed` feed the TUI and the advisor.
  (docs/TRAJECTORY_MODEL.md)
- **Evidence ledger.** A lightweight append-only table in `memory.db`: one record per command
  result, validation, interactive session and criterion verdict, with `supports` /
  `contradicts`. Every executed command becomes evidence; a declared failure signal observed
  in output contradicts the expectation even when the exit code says success.
  (docs/EVIDENCE_LEDGER.md)
- **Evidence-backed verification.** Every success criterion maps to ledger evidence and must
  reach PASS: no evidence → INSUFFICIENT_EVIDENCE (the run replans with the missing evidence
  named), contradiction without support → FAIL with the contradicting records cited, and the
  model judge rules only over the cited bundle. Each criterion verdict is itself recorded as
  evidence. (docs/VERIFICATION.md)
- **Repair as a distinct evaluator decision.** The direction is valid but the implementation
  is wrong → keep the candidate, tell the next attempt what to fix; the trajectory records
  the repair. ACCEPT / REPAIR / ROLLBACK / REPLAN are now distinct transitions.
- **Verified-progress stagnation.** Learning a *new* failure signature counts as progress
  (knowledge), repeating one does not (activity); the stagnation window now feeds on
  knowledge, not busyness.
- **Expectation before action.** Plan steps carry `expected_evidence` and `failure_signals`
  (the planner prompt asks for them; replanned steps get them from the failure reason); the
  controller context shows them; the runtime checks signals against observed output.
- **TUI trajectory semantics.** Trajectory verdicts on the timeline (accepted / rejected /
  repaired / replanned / blocked), running evidence counts in the validation panel
  (`3 supporting · 1 contradicting · 9 records`), contradictory evidence gets a semantic line.

### Changed / repaired

- **Knowledge retrieval is scoped to the source repository** (`source_repo` column, migrated):
  lessons accumulate across runs of one project without leaking into another project's
  decision context.
- **Pinned context is bounded**: user instructions and immutable records stay lossless; the
  last 8 failure lessons are pinned and the store keeps the rest retrievable — a cap on the
  projection, never on the truth.
- **Context duplication removed**: constraints and criteria were stored twice (header +
  pinned records); the request record is now the single canonical user-instruction record.
- **Evaluator-promoted memories capped** at 3 per step (model-authored advice, not ground
  truth). Sensitive-answer redaction no longer shreds output on short answers (whole echo
  lines are redacted instead of every occurrence). `estimate_tokens` corrected (chars/3).
- Dead code removed (`looks_interactive`).

### Tests

275 pass (+11 in `tests/test_hardening.py` + 4 TUI): invariants A–J as runtime behavior
(rejected execution never trusted; accepted work has evidence; rollback restores state but
keeps knowledge; context reconstructs without chat; pinned survives budget pressure;
strategy refusal; evidence-backed completion; source repo untouched; artifacts outside the
target), trajectory tests 3 (contradictory evidence forces repair), 4 (repeated strategy
refused and reconsidered) and 6 (final evidence mapping refuses and then passes), plus the
evidence ledger and migration tests.

### Docs

IMPLEMENTATION_AUDIT.md (the 0.4.0 audit), TRAJECTORY_MODEL.md, EXECUTION_KNOWLEDGE_SPLIT.md,
EVIDENCE_LEDGER.md, CONTEXT_RECONSTRUCTION.md, VERIFICATION.md, FAILURE_RECOVERY.md,
INTERACTIVE_EXECUTION.md; ARCHITECTURE, MEMORY_CONTEXT, TUI and TEST_PLAN refreshed; AGENTS.md
rewritten around the invariants.

## [0.3.1] — 2026-09-14

### Added

- **`gcae prune`** — run-data retention. Deletes the oldest run records beyond `--keep` (default
  10), with `--dry-run` to list first. Never touches repositories or worktrees, never deletes a run
  whose repository lock is held (a live run in another process), and skips unreadable records with a
  report instead of deleting them. A record whose merge is still recorded is kept (that record is
  the only place the pre-merge/merge commit pair lives, so `gcae undo` needs it) unless `--force`
  says to delete it anyway.

## [0.3.0] — 2026-09-14

### Added

- **Interactive programs are a first-class execution case.** `run_command` now runs commands in one
  of three modes: `batch` (stdin closed, so a program that reads stdin gets end-of-file instead of
  hanging), `scripted_input` (answers supplied up front, e.g. guesses for a game), and
  `interactive_pty` (a real pseudoterminal for programs that check `isatty()`, use `getpass`, or need
  a terminal). Children run unbuffered, in their own process group, and a prompt is *seen* while the
  program waits — the old behaviour (a bare `subprocess.run` with one timeout) reported
  "command timed out" for a program that was simply asking a question.
- **Typed failure evidence.** Every command result is classified (`interactive_input_required`,
  `command_timeout`, `code_error`, `test_failure`, `dependency_missing`, `file_not_found`,
  `permission_error`, `invalid_argument`, …) with a lesson and the evidence, stored as immutable
  memory and injected into the next controller prompt together with a directive naming the change to
  make. Partial output, mode, exit code, timeout kind and termination reason are captured instead of
  a bare "timeout".
- **Timeouts are separated**: startup (no output at all), idle (no progress while running), wall
  clock, and "waiting for input" — which is not a failure at all. Timed-out commands are terminated
  by process group (SIGTERM, then SIGKILL) and their partial output is kept.
- **Strategy ledger.** An approach (tool + arguments) that fails the same way twice on an unchanged
  candidate is refused *without executing*, with the lesson, so the next attempt must change the
  method. A retry after a real change (different arguments or a different candidate tree) is allowed.
- **Evidence gate.** When a step changed code and the run declares a runnable check (configured
  validation commands, a `command succeeds:` criterion, or a step requirement naming a command), the
  acceptance is refused until a command has actually run. The candidate is kept and the step returns
  to EXECUTE with the requirement recorded. `[runtime] require_execution_evidence = false` disables
  it, and file-artifact tasks are never blocked (the advice is recorded instead).
- **`waiting_for_user` for live processes.** A process waiting for a value only the user has pauses
  the run with `pending_input` persisted (command, prompt, mode, sensitivity), the process stays
  alive, and the TUI offers `i` to send the answer. `gcae input <repo> <run-id> <value>` does the
  same from the CLI. Sensitive prompts are never written to the event log and the terminal echo is
  redacted from the captured output.
- **`blocked` state** for a run with no safe autonomous path left: the reason, the last trusted
  checkpoint and what would unblock it are recorded; the CLI prints them and the TUI shows a banner.
- TUI: execution mode, queued answers and `INPUT REQUIRED` in ACTIVE; timeline entries for
  `failure_classified`, `interactive_detected`, `strategy_ineffective`,
  `execution_evidence_required`, `interactive_input_required`, `user_input_supplied` and `run_blocked`.

### Changed

- `tests/test_phase8.py`'s conflict fixtures now actually run the `command succeeds:` criterion they
  declare — the evidence gate made the previous "write a file and call it verified" trajectory
  impossible, which is the point of the gate.

## [0.2.1] — 2026-09-14

### Fixed

- **The dashboard no longer moves while the agent works.** The boxes above the flexible ones were
  content sized, so a model call, a new speculative file or a finished check resized a panel and
  pushed everything below it. Measured on a live run: the right column jumped three rows when ACTIVE
  changed shape (waiting → tool running), and again when the candidate's file list appeared. Every
  panel is now either fixed (OBJECTIVE 5 rows, ACTIVE 7, CHECKPOINT 6, VALIDATION 5) or flexible
  (`1fr`: PLAN, EVALUATION), and the timeline is fixed per breakpoint (4–11 rows) instead of growing
  as events accumulate. Content is windowed inside the boxes, so nothing on screen moves:
  - the ACTIVE box is constant across a call's shape changes (waiting → generating → tool → done),
  - the candidate file list is capped at two files plus `+ N more · press d` (it grows once, when
    speculative work appears, instead of growing per touched file),
  - the plan absorbs the left column's slack and the evaluation box absorbs the right column's,
  - a failed check can no longer be pushed out of VALIDATION by the checks above it.

### Added

- `tools/tui_demo.py --frames N --interval S`: prints consecutive frames of a live run together with
  every panel's geometry (x, y, width, height, body rows) and a telemetry scan of the timeline and
  ACTIVE bodies — the visual-QA counterpart to the automated check.
- `tests/test_tui.py::test_panel_boxes_never_move_while_a_model_streams`: samples the rendered app
  during a streaming call, asserts every box is identical while the run is live, that nothing moves
  afterwards (the completion banner may only resize the flexible boxes), that no telemetry string
  reaches the main screen, and that the samples actually covered the transitions (model generating,
  tool running, run finished) — otherwise the guard would prove nothing. Verified to fail when any
  fixed box is turned back into a content-sized one.

## [0.2.0] — 2026-09-14

### Changed

- **The dashboard shows state, not telemetry.** The main screen answers "what is being
  accomplished, with what, and how far did it get" in about three seconds; model streaming lives in
  the log screen where it belongs.
  - Provider events (`provider_started`, `provider_first_token`, `provider_progress`,
    `provider_waiting`) no longer create timeline entries. They used to fill the curated event list
    with `streaming controller · 1.2k chars · 3s` lines and push the semantic story out of view.
    The log screen keeps every one of them, now with role, model, character and reasoning counts and
    the streamed preview.
  - The ACTIVE panel no longer prints character counts or partial generations. It shows GOAL,
    ACTION (in human words — "Create src/model.py", not `create_file` + raw JSON), TARGET, WHY,
    EXPECTED and a single state row; a model call in flight is one calm line (`controller ·
    generating · 3.4s`) plus the model name.
  - The timeline is a bounded strip (3–10 rows) instead of a `1fr` panel. On a tall terminal the
    old layout turned the screen into a mostly empty log; now the two columns absorb the slack, so
    a taller terminal shows more plan steps and more checks.
  - `candidate_state` (which fires after every tool call) is announced **once per step** as
    `+ candidate changed · 2 files · +81 -4`; the panel itself always shows the live scope.
  - The evaluator's decision has its own EVALUATION section (decision word plus the stored reason)
    instead of a `decision` row inside VALIDATION.
  - VALIDATION is a structured check list (`✓`/`×`/`…`/`–`) that never dumps command output, and
    rows are ordered failures first so a short panel cannot hide the failing check. A failed
    criterion now carries its evidence.
  - The CHECKPOINT panel states the distinction in words: `TRUSTED <commit> <subject>`,
    `CANDIDATE DIRTY · N files · +A -D` with the file scope, `CLEAN · no speculative changes`, and
    `restored after rejection` after a rollback.
  - The top bar names the run state from the real phase (`PLANNING`, `ACTING`, `VALIDATING`,
    `EVALUATING`, `CHECKPOINTING`, `ROLLING BACK`, `VERIFYING`, `PAUSED`, terminal states) and keeps
    the elapsed time at every width.
  - Empty states are deliberate: `waiting for the first plan`, `– awaiting a candidate to validate`,
    `waiting for the first evaluation`, `waiting for the first semantic step`.
  - Panel row budgets follow the space the layout gives them, so panels fill with content instead of
    leaving holes at the bottom of a box.

### Removed

- `formatters.run_badge`, `formatters.thousands` and `ActivityPanel._stream_row` (dead after the
  redesign); the `original` label on the objective, replaced by `request` under an `OBJECTIVE`
  heading; the duplicated merge note in the timeline (the runtime's `merge_completed` event already
  reports it).

## [0.1.7] — 2026-09-14

### Fixed

- **`gcae run` no longer silently uses the fake provider.** Without `--config` the CLI built its
  config from defaults, whose provider kind is `fake` — so a real run failed with
  `provider output: fake provider trajectory is exhausted`, and it looked like a model problem. The
  CLI now discovers a configuration file (`$GCAE_CONFIG`, `./config.toml`,
  `~/.config/gcae/config.toml`), prints which one it used, and says explicitly when nothing was found
  and the fake default is about to be used.

### Added

- **The wiki is published** — 14 task-oriented pages under
  <https://github.com/mberkanbicer/gcae/wiki>, generated from `wiki/` with `tools/publish_wiki.sh`,
  and refreshed to describe 0.1.7 behaviour (self-recovery, streaming, retries, failover, the
  repository lock, degraded mode, configuration discovery).
- **Configuration discovery** for `gcae run` and `resume` (see *Fixed* below).
- **One run per repository.** `run`, `resume`, `merge` and `undo` take a per-repository lock before
  touching the source branch; a second run on the same repository is refused with the id of the run
  holding it. The lock file lives in the runtime directory and is released by the OS if the process
  dies. Two runs can no longer interleave two merges into one branch.
- **The dashboard explains every failure the CLI does.** The timeline now carries `model_failover`,
  `runtime_degraded`, `success_criteria_adopted`, `rollback_failed`, `repeated_failure` and the merge
  events (conflict detected/resolved/unresolved, merge completed); a degraded run keeps a `DEGRADED`
  flag in the status bar with the last lost subsystem.
- **Repeated identical rejections escalate the model.** After the same rejection three times the
  controller moves to `models.escalation` (once) and the failure streak resets, before the run would
  ask the user. The approach is exhausted; a stronger model is a better answer than a question.
- **An evaluator failover re-judges the same work.** When the evaluator's model is broken the step is
  no longer rejected on the way out: the swap happens and the work is judged again on the fallback.

### Changed

- **A state file that cannot be written now stops the run.** Degraded bookkeeping still covers memory
  and the event log, but a stale `state.json` makes `gcae resume` continue from a checkpoint the
  branch has already moved past — that is a correctness problem, not a resilience feature. The run
  fails with `run state could not be written: …`, accepted commits stay on the branch, and the event
  log still explains what happened.

## [0.1.6] — 2026-09-14

### Added

- **Model failover.** A provider outage was the one failure recovery could not fix, because the
  advisor would have to call the same broken endpoint. With `[models.escalation]` configured, a
  failing controller, planner, evaluator or verifier role now moves to that fallback model once per
  role and the run continues (`model_failover` event). If the fallback fails too, the ladder takes
  over. Verified live: an invalid controller model id → failover → run completed in 11 seconds.

## [0.1.5] — 2026-09-14

### Added

- **Self-recovery is now the default answer to every failure.** The ladder already covered
  stagnation, unusable provider/evaluator output, stalls, exhausted budgets and unexpected
  exceptions; it now also covers a **planner outage with no criteria** (the advisor may supply the
  criteria and the first step, recorded as `success_criteria_adopted`) and **transient network
  failures**, which are retried with exponential backoff, `Retry-After` support and jitter
  (`[provider] retries`, `retry_backoff`) before they are allowed to become a failure.

### Fixed

- **A failing recovery no longer kills the run it is rescuing.** Everything inside the advisor path
  is contained: an exception there is recorded as a failure memory and the caller's ladder
  continues, instead of propagating out of `run()`.
- **Bookkeeping failures can no longer end a run.** Writing `state.json`, appending to
  `events.jsonl` or recording memory now degrades instead of raising: the loss is recorded
  (`runtime_degraded` event, a bounded `degradations` list in `state.json`, a WARNING and a
  `degraded:` line in the CLI summary) and the work continues. Correctness gates are untouched —
  validation, evaluation and verification still have to pass.
- **A failed rollback is a recovery trigger, not a crash.** It is recorded (`rollback_failed`
  event, failure memory) and the ladder decides; the accepted commits are still on the branch, so
  nothing is lost.
- **A corrupt or half-written `state.json` explains itself** (`run state is corrupt or incomplete:
  … — the event log is intact: …`) instead of surfacing a JSON stack trace.
- **The CLI never dumps a traceback.** Unexpected errors print the type, the message, and the fact
  that the run directory and accepted commits are intact, with `gcae list` / `gcae resume` as the
  next step.

## [0.1.4] — 2026-09-14

### Fixed

- **Recovery from a checkpoint crashed the run it was trying to save.** The iteration budget is
  checked immediately after a step is accepted, so recovery could start while the phase was
  `CHECKPOINT` — and neither `checkpoint -> plan` nor `checkpoint -> rollback` is a legal state
  transition. The run died with `unexpected error: ValueError: invalid transition checkpoint ->
  plan`, and its own crash handler then started a second diagnosis of that error. Recovery now
  returns to planning through legal transitions (`checkpoint -> execute -> plan`), verified by a
  regression test that exhausts the budget exactly at an acceptance.
- **The diagnosis call was invisible.** The recovery advisor call was the only model call not
  wrapped in the progress reporter, so it produced no `provider_started` / `provider_progress` /
  `provider_finished` events: the dashboard showed nothing while it ran, and the event log could
  not account for the time. Traced from a live run whose log showed two `recovery_completed`
  events but only 11 provider calls.

## [0.1.3] — 2026-09-14

### Fixed

Three defects that turned a two-minute job into seventeen minutes of useless work, all found by
tracing a real run's event log:

- **Scope is evidence, not a gate.** A planner step whose `intended_scope` did not match the files
  the task required (observed: prose such as `"file creation"` in the scope) failed deterministic
  validation on every attempt, so each created file was rolled back and rewritten — 20 iterations,
  zero accepted steps. Creating a new file is now never a scope violation, out-of-scope edits to
  existing files are reported as a warning for the evaluator, non-path entries are ignored, and the
  planner prompt asks for repository-relative paths.
- **The candidate diff hid the agent's own work.** `git diff` skips untracked files and staged
  changes, so the agent could not see the file it had just created, concluded it was missing or
  incomplete, and rewrote it (observed: six rewrites of one complete 30-line script). The candidate
  diff is now the working tree against the last checkpoint, untracked files included.
- **A finished plan was diagnosed instead of verified.** The iteration-budget check ran before the
  "plan finished?" check, so a run that completed its plan exactly at the budget paid a
  self-diagnosis (63s) and a redundant re-planned step (177s) before verifying anyway. A finished
  plan is now verified regardless of the budget.

- **Rejection reasons name the failure.** The deterministic evaluator reported "deterministic
  validation failed"; it now names the failing command, whitespace errors, out-of-scope files and
  deletions, so the next attempt is not blind.

- **A repeated identical failure reaches the ladder early.** Three rejections with the same
  signature (an immutable failure memory, `repeated_failure` event) trigger the recovery ladder even
  when unrelated read-only steps are accepted in between.



### Added

- **Live model visibility.** Completions now stream (`[provider] stream`, default on) and every model
  call is bracketed by events: `provider_started`, `provider_first_token`, rate-limited
  `provider_progress` (content and reasoning character counts plus a preview tail), 10-second
  `provider_waiting` heartbeats and `provider_finished` with the wall-clock time. The dashboard's
  ACTIVE panel shows the live counts and preview, the timeline moves during a long deliberation, and a
  headless run logs the same events. Endpoints that refuse streaming, or answer with a buffered body,
  are handled automatically.

- **Hang detection.** `[provider] stall_timeout` (default 45s) bounds each read; a silent endpoint now
  fails with `provider request stalled: no data for Ns …` and enters the recovery ladder instead of
  holding the run. Verified against a real black-hole socket: 3s to detection with `stall_timeout = 3`
  (previously 63s, and indefinitely before this change).

- **Runs are discoverable while the planner thinks.** `state.json` is written before the first
  model call, so a run that is still planning appears in `gcae list`, can be resumed, and is not
  orphaned if the process is interrupted during planning.

- **Readable long steps.** The plan panel wraps the active step across up to three rows (neighbours
  yield to it), the objective and current-goal rows gained the same room, and the ACTIVE panel has an
  extra row for the live stream line — so a long step goal is legible instead of being elided to one
  line.

## [0.1.1] — 2026-09-14

### Added

- **Self-recovery**: a run that is about to fail — stagnation, unusable provider or evaluator
  output, or an exhausted step budget — now reads its own persisted trace (filtered events,
  validation and verification evidence, failure memories, candidate diff) and asks the configured
  recovery model for a structured diagnosis: root cause, one corrective instruction, and a
  strategy. A `replan` strategy queues the correction as the next semantic step and grants extra
  iterations; `ask_user` and `stop` end the run honestly. Bounded by `[runtime] recovery_attempts`
  (default 2) with `[runtime] recovery_budget` (default 5) extra iterations per correction, and a
  diagnosis that cannot be parsed leaves the old ladder intact. New `[models.recovery]` role
  (defaults to the controller's model, i.e. the escalated one after escalation), new
  `recovery_started` / `recovery_completed` / `recovery_failed` events in the dashboard timeline,
  and `gcae inspect` prints the last diagnosis. An unexpected exception inside the loop is
  handled the same way instead of escaping the process: it is recorded as an immutable failure
  memory, handed to the advisor, and only then allowed to end the run.

- **Stagnation ladder** — an exhausted approach now stores a "change hypothesis" memory, escalates to
  the configured stronger model, and then asks the user (`waiting_for_user`) instead of stopping the
  run. Only a run that was already asked and still makes no progress fails, and its accepted
  checkpoints are still delivered.

### Fixed

- An accepted step that changed no file no longer counts as stagnation: three read-only accepted
  steps used to kill a healthy run with `execution stagnated`.
- An instruction submitted while the loop was not running was queued into nothing; it now revives
  the stalled run, applies the instruction and continues from the accepted state.
- A stalled run now reports the question, its accepted steps and the resume command in the CLI
  summary and the dashboard.

## [0.1.0] — 2026-09-13

First public release. GCAE executes one coding task at a time inside an isolated Git worktree and
only keeps work that deterministic validation, evaluation and final verification accept.

### Added

- **Semantic-step execution loop** — `PLAN → SEMANTIC STEP → EXECUTE → OBSERVE → VALIDATE →
  EVALUATE → ACCEPT | ROLLBACK | REPLAN → VERIFY`. The unit of progress is a verified semantic step,
  not a tool call, with a per-step tool budget and a repetition guard.
- **Isolated Git worktree per run** — one branch (`gcae/<run-id>`) and one worktree per run, created
  from a committed base. Rejection performs `reset --hard` plus cleanup inside that worktree only.
- **Reversible merges** — the verified branch is merged into the current branch and the pre-merge and
  merge commits are recorded, so `gcae undo` restores the previous state. The run's own worktree is
  removed after merging while the branch is kept, so undo and re-merging stay possible.
- **Memory that survives rollback** — SQLite (FTS5) store for facts, decisions, observations, failure
  lessons and immutable user instructions, promoted by the evaluator and re-read on replan.
- **LLM planner and hybrid verifier** — the planner derives the objective and at least one checkable
  success criterion from the request; criteria are verified deterministically first, with an opt-in
  model judge that fails closed on provider errors, missing evidence or a negative verdict.
- **Structured decision providers** — OpenAI-compatible HTTP providers with schema-validated
  decisions, bounded repair, JSON-mode control and reasoning-model recovery; plus local endpoints
  (Ollama) and deterministic offline providers for tests.
- **Terminal dashboard** — run identity, objective/current goal, plan progress, active tool, trusted
  checkpoint versus candidate, validation results, context and memory metrics, curated event
  timeline, plus diff, log, memory, context, plan and evaluation viewers.
- **Git automation owned by the loop** — base-commit bootstrap for unborn or dirty repositories, a
  fallback commit identity, automatic merging, rescue of accepted checkpoints from failed runs, and
  agent-driven merge-conflict resolution in the run's own worktree.
- **CLI** — `run`, `resume`, `list`, `inspect`, `merge`, `undo`, with `--tui/--headless`,
  `--criterion`, `--constraint`, `--merge/--no-merge` and `--no-auto-bootstrap`.
- **Documentation** — architecture, state machine, Git execution, memory and context, tools,
  providers, TUI, CLI, configuration, workspace hygiene, test plan and an implementation audit.
- **CI** — ruff, strict mypy and the full test suite on Python 3.12 and 3.13, sdist/wheel builds with
  a clean-environment install check, and tag-driven GitHub releases.

### Fixed

- Blank or truncated provider responses are now diagnosed and recovered: blank content retries
  without `response_format`, truncated JSON retries with a larger output budget, and provider errors
  name `finish_reason`, reasoning size, completion tokens and the model.
- `git user.name`/`user.email` no longer block a run: commits fall back to `GCAE <gcae@localhost>`.
- Untracked directories are reported at file level, so scope checks, candidate line counts and diffs
  are accurate.
- A criterion that runs pytest no longer fails its own run's hygiene check, and generated caches can
  never reach a verified checkpoint.
- `file contains exactly` ignores a trailing newline at end of file (content still has to match).
- Failed runs exit non-zero, a run whose merge did not happen exits non-zero, and an empty run leaves
  no worktree behind.
- Conflicts can no longer be committed or merged: unresolved markers fail validation and block
  verification, and a rejected resolution aborts the merge and leaves the branch as it was.

[Unreleased]: https://github.com/mberkanbicer/gcae/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mberkanbicer/gcae/releases/tag/v0.1.0
