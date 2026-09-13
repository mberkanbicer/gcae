# Architecture

GCAE is a single-agent, reversible execution runtime with explicit Python orchestration. There is
no workflow framework and no multi-agent layer: planner, controller, evaluator and verifier are
roles inside one runtime, each a deterministic component or a structured call to the configured
model.

## Components

| Module | Responsibility |
| --- | --- |
| `runtime.py` | lifecycle, semantic-step loop, checkpoint/rollback, events, control, overrides |
| `state_machine.py` | explicit phase transitions |
| `models.py` | all cross-subsystem Pydantic contracts |
| `persistence.py` | atomic JSON state |
| `planner.py` | deterministic planner and model-backed `InitialPlan` planning |
| `controller.py` | builds the decision prompt; returns validated `Decision` |
| `evaluator.py` | deterministic evaluator and optional LLM evaluator |
| `verifier.py` | final success-criteria verification |
| `validation.py` | deterministic evidence before evaluation |
| `context.py` | context reconstruction, pinned sections, token budgeting |
| `memory.py` | SQLite FTS5 knowledge store, JSONL event log |
| `tools.py` | sandboxed tool registry |
| `git.py` | worktree, checkpoint, rollback, merge, undo |
| `safeguards.py` | repetition, stagnation, hygiene |
| `providers.py`, `http_provider.py` | structured-output providers |
| `cli.py` | run/resume/list/inspect/merge/undo, headless and TUI modes |
| `tui/` | Textual dashboard; consumes runtime events |

## Execution state vs knowledge state

Execution state is reversible: Git commits inside one isolated worktree per run. An accepted
evaluation creates a checkpoint; rejection performs `reset --hard accepted_commit` plus cleanup of
speculative files, strictly inside that worktree. Knowledge state is cumulative: facts, decisions,
failures, observations and user instructions live in SQLite and survive rollbacks and runs. The
user's working tree is never modified; a dirty source repository is refused.

## Semantic-step loop

```
load state ──► current plan step
      │
      ▼
  build context (fresh, budgeted, pinned)  ◄────────────────┐
      │                                                     │
      ▼                                                     │
  controller decision                                       │
   ├─ execute_tool ......... run tool, observe, persist ────┤
   ├─ complete_semantic_step ──┐                            │
   ├─ replan ................ rollback + new step ──────────┤
   ├─ finish_candidate ...... final verification            │
   └─ ask_user .............. persist and wait              │
                               │                            │
                               ▼                            │
                 deterministic validation                   │
                               │                            │
                               ▼                            │
                 evaluator: accept / rollback /             │
                            replan / continue /             │
                            finish_candidate ───────────────┘
                               │
                 accept ──► checkpoint commit (+ memory promotion)
                 rollback ─► reset to accepted commit, failure memory kept
```

A semantic step may contain several tool calls. Deterministic validation and evaluation run only
when the controller declares the step complete or `max_tool_calls_per_step` is exhausted, so read
operations never create checkpoints. Each step has a finite tool budget; the outer loop is bounded
by `max_steps`.

## Replanning and safeguards

Replanning discards speculative changes, keeps accepted commits and knowledge, and preserves
completed steps. Identical tool calls are blocked after `repetition_limit`; repeated
non-progressing iterations trip `stagnation_window`; two consecutive rejected steps escalate
controller decisions to `models.escalation` when configured.

## Final verification and completion

`finish_candidate` enters a separate deterministic verifier that checks every success criterion and
runs a hygiene pass. If verification passes while the worktree still holds uncommitted changes, the
runtime commits `gcae: verified final state` first, so `accepted_commit` always equals the verified
tree. Unsupported criteria fail closed.

## Events and UI

Every phase, decision, tool result, validation, evaluation, checkpoint, rollback, replan, override
and verification is a typed `Event` appended to `runs/<run-id>/events.jsonl` and pushed to
in-process subscribers. The Textual TUI subscribes from its UI thread and drives pause/resume/stop
and overrides through a thread-safe `RuntimeControl`. The engine works without the TUI.

## Runtime storage

```
${XDG_STATE_HOME:-~/.local/state}/gcae/
  memory.db                      # cumulative knowledge, all runs
  runs/<run-id>/
    state.json                   # resumable execution state
    events.jsonl                 # append-only event history
    tool-results/                # per-call tool results
    diffs/                       # per-step candidate diffs
    artifacts/                   # externalized large command outputs
  worktrees/<run-id>/            # one isolated worktree per run
```

Target repositories never receive runtime files.
