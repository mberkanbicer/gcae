# AGENTS.md — working on GCAE

Operational instructions for coding agents working **on this repository**. Detailed design lives in
`docs/`; this file states what must stay true.

## What this project is

A lightweight, single-agent, reversible execution runtime. One process drives a model through
semantic steps in an isolated Git worktree, validates results deterministically, evaluates them,
checkpoints accepted work, rolls back rejected work, and keeps knowledge in SQLite across rollbacks
and runs. It runs headless or with an interactive Textual TUI.

## Architectural invariants (do not break)

1. **Execution state is reversible, knowledge is cumulative.** Git checkpoints are the trusted
   state; memory (`memory.db`) survives every rollback. Deleting failure lessons on rollback is a
   bug.
2. **Exactly one worktree per run**, under the runtime directory. Never one worktree per step.
   Never modify the user's working tree. A repository that is unborn or dirty is repaired before
   the run: GCAE creates the base commit it needs (`auto_bootstrap`, bounded to 2000 files / 50 MB,
   `.gitignore` respected, reported as a notice), while a mid-merge/rebase repository and a
   non-repository directory are refused.
3. **Verified work reaches the user.** A completed run merges its branch into the source
   branch (`[runtime] auto_merge`, default on) with the pre-merge and merge commits recorded in
   `state.json`, so the work is visible in the checkout and `gcae undo` reverses it. A run that
   cannot be merged (dirty checkout, branch moved, nothing to merge) reports why and leaves the
   branch intact for `gcae merge`.
4. **Only acceptance creates commits.** An accepted step commits `gcae: <goal>`; a passing final
   verification with a dirty worktree commits `gcae: verified final state`; rejection does
   `reset --hard accepted_commit` plus cleanup inside the worktree only.
5. **The model never runs Git checkpoint commands.** Checkpoint management is runtime-owned and
   `git reset|clean|commit|worktree|...` stays blocked in the command tool.
6. **The unit of progress is a semantic step**, not a tool call. Tools run freely inside a step;
   deterministic validation and evaluation happen only at `complete_semantic_step` or when
   `max_tool_calls_per_step` is exhausted.
6. **Context is reconstructed per call** from persistent state. Never accumulate a growing
   conversation, never summarize summaries. Pinned data (request, constraints, criteria, current
   goal, accepted commit, failure lessons) cannot be dropped by budgeting.
7. **Structured decisions only.** Controller, planner and evaluator outputs are Pydantic models
   with bounded repair; invalid output fails the run — it never falls back silently.
8. **The TUI is first-class and must keep working headlessly.** The engine must not import
   Textual; the TUI subscribes to runtime events and drives a thread-safe `RuntimeControl`.

## Dependencies

Runtime dependencies are exactly `pydantic`, `httpx`, `textual`. Adding another requires a concrete
justification and a note in `docs/CONFIGURATION.md`. No agent frameworks (LangGraph/LangChain/etc.),
no vector databases, no embeddings, no message brokers.

## Layout

```
src/gcae/          runtime, contracts, memory, context, tools, validation, evaluator,
                   verifier, planner, providers, config, cli, git
src/gcae/tui/      Textual app (isolated; engine never imports it)
tests/             architecture tests, TUI tests, mandatory end-to-end trajectory test
docs/              architecture and subsystem documentation
```

## Required commands

```bash
.venv/bin/pytest -q          # full suite must pass
.venv/bin/ruff check .       # must be clean
.venv/bin/mypy src/gcae      # strict, must be clean
```

Run all three before considering any change complete. Add tests for behavior changes:
`tests/test_integration_rollback.py` (mandatory bad→rollback→good→verify trajectory) must keep
passing, and TUI changes must keep `tests/test_tui.py` passing.

## Hygiene rules for your own changes

- Modify existing modules; do not add parallel implementations or compatibility layers.
- No dead code, no unused imports/options, no speculative abstractions or factories.
- Keep the runtime loop readable in `runtime.py`; do not introduce workflow engines.
- Runtime data lives under `${XDG_STATE_HOME:-~/.local/state}/gcae`; never write runtime state
  into target repositories or this repository.
- Do not commit `config.toml` (API keys) or run artifacts.
- Document behavior that ships; delete documentation for behavior that does not exist.
