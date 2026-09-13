# Test plan

## Suites

| File | Proves |
| --- | --- |
| `test_phase1.py` | Pydantic contracts, state round-trip, explicit transitions, bounded provider repair |
| `test_phase2.py` | worktree isolation, dirty-source refusal, checkpoint/rollback, generated-artifact cleanup, reversible merge/undo |
| `test_phase3.py` | immutable memory, FTS retrieval and refresh, pinned context under budget, JSONL events |
| `test_phase4.py` | workspace escape rejection, dangerous-command rejection, file tools, patch path checks |
| `test_phase5.py` | runtime completion/checkpointing, premature-completion blocking, memory promotion, lifecycle logging |
| `test_phase6.py` | repetition, stagnation, hygiene detection |
| `test_phase7.py` | OpenAI-compatible provider contract, controller prompt contract |
| `test_phase8.py` | config loading, XDG state dir, provider/evaluator kind resolution, resume, CLI merge/undo guards |
| `test_verifier.py` | deterministic criteria, fail-closed unsupported criteria, hybrid judge (evidence required, provider failures fail closed, worktree samples reach the judge) |
| `test_semantic_steps.py` | tool calls batched inside a step, step tool budget, read-only steps, repeated actions force evaluation, replan discards speculative work, `write_file`/`run_tests`, artifact externalization |
| `test_control.py` | live event subscribers, pause until resume, stop, immutable user override, queued instructions drained while paused |
| `test_routing.py` | role-model resolution, planner kinds, verifier kinds, LLM planner merge rules, escalation after repeated failures, hybrid verification end to end, evaluator failure handling |
| `test_context_budget.py` | pinned data survives trimming, low-priority records dropped but retrievable, long-run records intact |
| `test_cli_commands.py` | `list` and `inspect` output and ordering |
| `test_tui.py` | dashboard rendering and completion, interactive request screen with planner-derived criteria, pause/resume/stop keys, override modal, diff modal, event-driven panels |

## Mandatory end-to-end scenarios

1. **Rollback trajectory** (`test_integration_rollback.py`): temporary Git repository, fake model
   creates a bad implementation, evaluator rejects it, the runtime rolls back, the failure lesson
   survives, a different correct implementation is accepted, a checkpoint commit is created and the
   verifier passes. Assertions cover: untouched source tree, isolated worktree, rejected file gone,
   `accepted_commit` unchanged while the rejected step was evaluated, failure memory present,
   `HEAD == accepted_commit`, no temporary artifacts.
2. **Context budget** (`test_context_budget.py`): a trajectory long enough to exceed the budget —
   the context stays within budget, pinned sources remain, dropped records stay retrievable and the
   persistent history is unchanged.
3. **TUI lifecycle** (`test_tui.py`): run start through completion renders in the dashboard;
   rollback/replan events and final status are visible; pause, resume, stop, override and diff
   inspection all work through the pilot.

## Commands

```bash
.venv/bin/pytest -q          # all suites, includes TUI tests (Textual pilot)
.venv/bin/ruff check .
.venv/bin/mypy src/gcae
```
