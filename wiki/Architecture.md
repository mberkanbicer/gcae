# Architecture

## Modules

| Module | Responsibility |
| --- | --- |
| `cli.py` | argument parsing, config loading, run/resume/list/inspect/merge/undo, headless and TUI entry points |
| `config.py` | TOML schema (pydantic), XDG state defaults |
| `runtime.py` | the execution loop, lifecycle, event emission, merge orchestration |
| `state_machine.py` | allowed phase transitions, enforced on every transition |
| `models.py` | pydantic contracts: state, plan, decisions, validation, evaluation, verification, events |
| `planner.py` | deterministic planner and the model-backed planner (criteria derivation) |
| `controller.py` | turns context into one structured decision per iteration |
| `evaluator.py` | deterministic and model-backed step evaluation |
| `verifier.py` | final criterion verification, optional strict model judge |
| `validation.py` | deterministic evidence collection |
| `context.py` | context reconstruction under a token budget |
| `memory.py` | SQLite FTS5 memory and the append-only JSONL event log |
| `git.py` | worktrees, checkpoints, rollback, merge, undo, bootstrap, conflict plumbing |
| `tools.py` | tool registry (file tools, search, `run_command` with a blocklist) |
| `safeguards.py` | repetition guard, stagnation window, hygiene check |
| `providers.py`, `http_provider.py` | provider contract, OpenAI-compatible HTTP provider |
| `tui/` | the Textual dashboard: presentation reducer, widgets, viewers, dialogs |
| `persistence.py` | run state store |

## Data flow of one iteration

```
memory + state + diff ──► ContextBuilder ──► controller model ──► Decision
                                                                    │
                       tool execution ◄──────────────────────────────┘
                              │
                       Observation ──► memory (FTS5) + working memory
```

Validation and evaluation only run when a step is declared complete (or when the repetition guard or
the step budget forces a decision), which keeps the loop cheap and the checkpoints meaningful.

## Two kinds of state

| | Execution state | Knowledge state |
| --- | --- | --- |
| Where | Git commits on `gcae/<run-id>` | `memory.db` (SQLite FTS5) |
| Lifetime | reversible, discarded on rollback | cumulative, survives rollback and resume |
| Read by | Git, the verifier, the merge step | the context builder, every model call |

## Event model

Every meaningful transition is an `Event` appended to `runs/<run-id>/events.jsonl` and delivered to
in-process subscribers. The dashboard is just another subscriber, which is why the engine never
imports Textual and works headlessly.

## Invariants

1. Execution state is reversible; knowledge is cumulative.
2. Exactly one worktree per run; never modify the user's working tree.
3. The loop owns every Git operation, conflicts included.
4. A blocked run asks instead of dying.
5. Verified work reaches the user through a recorded, reversible merge.
6. Only acceptance creates commits.
7. The model never runs Git checkpoint commands.
8. The unit of progress is a verified semantic step, not a tool call.
9. Context is reconstructed per call from persistent state.
10. Structured decisions only: every model output is schema-validated.
11. The TUI is first-class and must keep working headlessly.

The long form of each invariant lives in
[`AGENTS.md`](https://github.com/mberkanbicer/gcae/blob/main/AGENTS.md).
