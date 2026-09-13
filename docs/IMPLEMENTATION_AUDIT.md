# Implementation Audit

Audit of the repository at `fd45358` (43 tests, ruff and mypy clean) against the target
architecture: a single-agent, reversible, semantic-step execution runtime with persistent
knowledge, adaptive replanning, local/OpenRouter model support, headless CLI and an interactive
TUI.

## Current implementation

| Component | Files | Verdict |
| --- | --- | --- |
| Typed contracts | `models.py` | KEEP, extend (working memory, runtime events, tool artifacts) |
| State machine | `state_machine.py` | KEEP, extend (step-status transitions) |
| Runtime loop | `runtime.py` | REPAIR: one tool call per evaluation cycle instead of semantic steps |
| Git isolation | `git.py` | KEEP (worktree per run, checkpoint, rollback, merge, undo already verified) |
| Persistent memory | `memory.py` | KEEP (SQLite FTS5, immutable records, shared across runs) |
| Context builder | `context.py` | KEEP, extend (working memory, artifact references, budget telemetry) |
| Tools | `tools.py` | REPAIR, extend (write_file, run_tests, large-output externalization, duration) |
| Validation | `validation.py` | KEEP, extend (diff stat, scope warnings) |
| Planner | `planner.py` | REPAIR: deterministic single step; needs model-backed InitialPlan |
| Controller | `controller.py` | KEEP, rename actions to the target contract |
| Evaluator | `evaluator.py` | KEEP (deterministic default, optional LLM) |
| Verifier | `verifier.py` | KEEP (deterministic, fail-closed) |
| Safeguards | `safeguards.py` | KEEP |
| Provider | `providers.py`, `http_provider.py` | KEEP, extend (role models, escalation) |
| Config | `config.py` | KEEP, extend (step budgets, role models, planner kind) |
| CLI | `cli.py` | REPAIR: run/resume/undo/merge exist; list, inspect, headless/TUI modes missing |
| TUI | `tui/` | REPAIR: dashboard implemented (see `docs/TUI.md`); the audit below records the starting state |
| Runtime events for subscribers | `memory.py` `EventLog` | REPAIR: JSONL only, no live in-process subscribers |
| User overrides during a run | — | MISSING |
| Working memory | — | MISSING |
| Tests | `tests/` | KEEP, extend (semantic steps, override, TUI, long-context, CLI) |
| Docs | `docs/` | KEEP, extend (TUI, CLI, configuration, audit) |
| `AGENT.md` | root | REPLACE with an operational `AGENTS.md` |

## Gap analysis

1. **Semantic step.** The target unit is a multi-tool semantic step evaluated when the model
   declares it complete (`complete_semantic_step`) or the per-step tool budget is exhausted. The
   current loop evaluates and potentially checkpoints after *every* tool call, including reads.
2. **Controller actions.** Target: `execute_tool`, `complete_semantic_step`, `replan`,
   `finish_candidate`, `ask_user`. Current: `execute_tool`, `continue`, `replan`, `finish`,
   `ask_user`.
3. **Live event stream.** Target: runtime emits typed events to an in-process subscriber for the
   TUI, sharing the JSONL event model. Current: JSONL append only.
4. **TUI.** Missing entirely. Target: Textual dashboard with run header, objective, plan, Git
   state, current action, validation, memory, context usage, model, event log, failure/rollback
   visibility; pause/resume/stop, diff view, user override; tested with Textual pilot.
5. **User overrides.** Target: inject a new immutable instruction into an active run, roll back
   speculative work, replan, expose through CLI/TUI. Missing.
6. **Working memory.** Target: small per-step structure (hypotheses, active files, blocker,
   findings, pending validations), promoted to persistent memory at step end. Missing.
7. **Planner.** Target: analyze the request, infer objective/criteria/constraints/assumptions and
   an initial plan. Current planner is deterministic and only wraps the caller's inputs.
8. **Model routing.** Target: configurable role models (planner, controller, evaluator, verifier,
   escalation) falling back to one default model. Current: one provider plus evaluator selection.
9. **Tool surface.** Target adds write/patch of existing files and a test runner, plus external
   storage of very large command outputs with a context reference. Current lacks `write_file`,
   `run_tests` and output externalization.
10. **CLI.** Target adds `list` and `inspect`, plus explicit `--headless`/TUI modes. Current has
    `run`, `resume`, `merge`, `undo`, always non-interactive.
11. **Documentation.** TUI, CLI and configuration references are missing; `AGENT.md` describes an
    older contract.

## Removal / replacement list

- Replace per-tool evaluation with semantic-step evaluation (keep deterministic validation, keep
  the checkpoint/rollback protocol).
- Rename controller actions; update all callers and tests. No aliases retained.
- Replace `AGENT.md` with an operational `AGENTS.md`; keep detailed design in `docs/`.
- Do not retain any parallel implementation: the existing runtime loop is repaired in place.

## Non-goals preserved from the current design

- No agent framework, no vector store, no embeddings, no multi-agent orchestration.
- Runtime state stays under `${XDG_STATE_HOME:-~/.local/state}/gcae`; target repositories are
  never polluted.
- Only one worktree per run; accepted commits remain the trusted state; knowledge survives
  rollback.
