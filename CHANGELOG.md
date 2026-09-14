# Changelog

All notable changes to GCAE are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

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
