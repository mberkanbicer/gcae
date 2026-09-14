# Changelog

All notable changes to GCAE are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

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
