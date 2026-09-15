# AGENTS.md — working on GCAE

Operational instructions for coding agents working **on this repository**. Detailed design lives in
`docs/`; this file states what must stay true.

## What this project is

GCAE is **not another generic coding agent**. It is a lightweight, single-agent,
**evidence-driven execution runtime**: actions are speculative, progress must be verified,
failed execution trajectories are reversible, and the knowledge gained from both success and
failure persists across replanning. One process drives a model through semantic trajectory
steps in an isolated Git worktree, validates results deterministically, evaluates them,
checkpoints accepted work, rolls back rejected work, and keeps knowledge in SQLite across
rollbacks and runs. It runs headless or with an interactive Textual TUI that is a live
trajectory inspector, not a chat client.

The loop is:

```
RECONSTRUCT → ACT → OBSERVE → JUDGE → COMMIT OR REVERT → LEARN → (reconstruct)
```

## Architectural invariants (do not break)

1. **Execution state is reversible, knowledge is cumulative.** Git checkpoints are the trusted
   state; memory (`memory.db`) survives every rollback. Rejection resets the worktree to the
   accepted commit; the failure lesson and its evidence records stay. Deleting failure lessons
   on rollback is a bug. Tested: `tests/test_hardening.py` (invariants A–J).
2. **Exactly one worktree per run**, under the runtime directory. Never one worktree per step.
   Never modify the user's working tree. A repository that is unborn or dirty is repaired
   before the run (`auto_bootstrap`, bounded to 2000 files / 50 MB, `.gitignore` respected,
   reported as a notice); a mid-merge/rebase repository and a non-repository directory are
   refused.
3. **The loop owns every git operation.** Conflicts included: a conflicting merge is brought
   into the run's own worktree, resolved by the agent as a normal semantic step, re-verified
   and retried; markers are never committed. Bootstrap, branches, worktrees, checkpoints,
   merges and cleanup are performed by the runtime without asking the user to run git.
4. **Every failure is diagnosed before it can end a run.** Self-recovery is the default
   response: stagnation, unusable provider/evaluator output, stalls, exhausted budgets,
   planner outages, unexpected exceptions and transient network errors all reach the advisor
   (bounded retries and backoff first, degraded mode for bookkeeping failures). Only an
   already-asked run, an advisor decision to stop, or an unusable diagnosis fails a run. See
   docs/FAILURE_RECOVERY.md for the full taxonomy and ladder.
5. **A blocked run asks after diagnosing.** Stagnation, unusable provider output and an
   exhausted step budget first trigger `recovery.py`: the advisor reads the run's own trace
   and returns a structured `Diagnosis`. `replan` queues the correction and grants bounded
   extra iterations; otherwise the run pauses (`waiting_for_user`) or blocks (`blocked`) with
   a question, keeping the plan and every accepted commit. A run only fails when the user was
   asked or recovery was exhausted.
6. **Verified work reaches the user.** A completed run merges its branch into the source
   branch (`[runtime] auto_merge`, default on) with the pre-merge and merge commits recorded
   in `state.json`; `gcae undo` reverses it. A run that cannot be merged reports why and
   leaves the branch intact for `gcae merge`.
7. **Only acceptance creates commits.** An accepted step commits `gcae: <goal>`; a passing
   final verification with a dirty worktree commits `gcae: verified final state`; rejection
   does `reset --hard accepted_commit` plus cleanup inside the worktree only. A checkpoint
   means *verified enough to trust*; an uncommitted candidate is *speculative* — the TUI and
   the docs use exactly those words.
8. **The model never runs Git checkpoint commands.** Checkpoint management is runtime-owned
   and `git reset|clean|commit|worktree|...` stays blocked in the command tool.
9. **The unit of progress is a semantic trajectory step**, not a tool call. Tools run freely
   inside a step; deterministic validation and evaluation happen at `complete_semantic_step`
   or when `max_tool_calls_per_step` is exhausted. **Action is not progress**: stagnation is
   measured by accepted checkpoints, verified criteria, new failure lessons and invalidated
   hypotheses — never by activity count.
10. **Progress must be proven.** The evidence ledger (`memory.db`, `evidence` table) records
    command results, validation, interactive sessions and criterion verdicts with
    supports/contradicts. Completion requires PASS for every success criterion, each mapped
    to ledger evidence; no evidence means INSUFFICIENT, contradiction means FAIL. The
    evaluator may say *repair* (keep the candidate, fix it) instead of rollback, and *replan*
    when the path is invalid — the four transitions are distinct.
11. **The plan is trajectory state, not disposable text.** Verified completed steps keep
    stable IDs, checkpoint linkage and locks across replans; replans are deterministic
    partial patches to the affected region (current + future by default), never full
    rewrites. A completed step is reopened only as `invalidated` with a recorded reason
    and ledger evidence, and dependents follow. Rollback to an older checkpoint
    invalidates exactly the steps that no longer survive it. Tested:
    `tests/test_plan_history.py`. See `docs/PLAN_HISTORY.md`.
11. **Context is reconstructed per call** from persistent state — never a growing
    conversation, never summarized summaries. Pinned data (request, constraints, criteria,
    current goal, expectation, accepted commit, latest user instructions, last 8 failure
    lessons) cannot be dropped by budgeting; the store keeps everything else retrievable.
    Retrieval is scoped to the source repository.
12. **Structured decisions only.** Controller, planner, evaluator and verifier outputs are
    Pydantic models with bounded repair; invalid output fails the run — it never falls back
    silently. The model's confidence claims are ignored; only observable evidence counts.
13. **The TUI is first-class and must keep working headlessly.** The engine must not import
    Textual; the TUI subscribes to runtime events and drives a thread-safe `RuntimeControl`.
    It shows trusted past, speculative present and adaptive future; raw model streaming stays
    in the logs.

## Dependencies

Runtime dependencies are exactly `pydantic`, `httpx`, `textual`. Adding another requires a
concrete justification and a note in `docs/CONFIGURATION.md`. No agent frameworks
(LangGraph/LangChain/etc.), no vector databases, no embeddings, no message brokers, no
multi-agent orchestration, no plugins.

## Layout

```
src/gcae/          runtime, contracts, memory, context, tools, validation, evaluator,
                   verifier, planner, providers, config, cli, git, execution
src/gcae/tui/      Textual app (isolated; engine never imports it)
tests/             architecture tests, TUI tests, trajectory tests, invariant tests
docs/              architecture and subsystem documentation
```

## Required commands

```bash
.venv/bin/pytest -q          # full suite must pass
.venv/bin/ruff check .       # must be clean
.venv/bin/mypy src/gcae      # strict, must be clean
```

Run all three before considering any change complete. Add tests for behavior changes:
`tests/test_integration_rollback.py`, `tests/test_adaptive_trajectory.py`,
`tests/test_hardening.py` (invariants A–J and trajectory tests 3, 4, 6) and
`tests/test_plan_history.py` (stable prefix, invalidation, resume, TUI history) must keep
passing, and TUI changes must keep `tests/test_tui.py` passing.

## Hygiene rules for your own changes

- Modify existing modules; do not add parallel implementations or compatibility layers.
- No dead code, no unused imports/options, no speculative abstractions or factories.
- Keep the runtime loop readable in `runtime.py`; do not introduce workflow engines.
- Runtime data lives under `${XDG_STATE_HOME:-~/.local/state}/gcae`; never write runtime
  state into target repositories or this repository.
- Do not commit `config.toml` (API keys) or run artifacts.
- Document behavior that ships; delete documentation for behavior that does not exist.
