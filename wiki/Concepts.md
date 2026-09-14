# Concepts

## Semantic step

A **semantic step** is one meaningful unit of progress — "add the health endpoint", not "call
`write_file`". Inside a step the agent may call many tools in any order. Validation and evaluation
happen either when the agent calls `complete_semantic_step` or when it exhausts
`max_tool_calls_per_step`.

The unit of progress is the verified step, not the tool call.

## Trusted state and speculative state

| | Trusted | Speculative |
| --- | --- | --- |
| What | commits accepted by validation + evaluation | the working tree between checkpoints |
| Where | `gcae/<run-id>` branch, `accepted_commit` | the run's worktree, untracked files included |
| On rejection | untouched | `reset --hard accepted_commit` + `clean -fdx` inside the worktree |

## Run phases

```
PLAN → EXECUTE → OBSERVE → VALIDATE → EVALUATE → ACCEPT / ROLLBACK / REPLAN → VERIFY
```

`state.json` records the phase, so a stopped or crashed run resumes where it was. Illegal
transitions are refused rather than guessed (a recovery from `checkpoint` re-enters through
`execute`).

## Validation, evaluation, verification

| Stage | Question | Who answers |
| --- | --- | --- |
| Validation | did the step break anything, is it in scope, do the commands pass? | deterministic code |
| Evaluation | did the step advance the objective? | deterministic evaluator, or a model (`hybrid`) |
| Verification | are all success criteria met in the final tree? | deterministic criteria, plus an optional model judge that fails closed |

Validation is evidence, not a gate: out-of-scope edits and new files are recorded and handed to the
evaluator instead of silently failing the step.

## Success criteria

Criteria are how "done" becomes checkable:

```
file exists: docs/API.md
file contains: app.py :: /health
file contains exactly: Makefile :: test:
command succeeds: python -m pytest -q
```

If a run has no criteria the planner derives them, and a run that cannot be verified is never
declared complete.

## Self-recovery

A run that cannot make progress **changes strategy before it gives up**: it records a decision
memory ("the previous approach is exhausted"), escalates to the stronger model, diagnoses its own
trace (`events.jsonl`, validation evidence, failure lessons, the candidate diff) and only then asks
you, and only then fails. Recovery is bounded (`recovery_attempts`, `recovery_budget`) and never
replaces evidence: a correction is an ordinary semantic step that still has to pass every gate.

See [Architecture](Architecture) for the complete failure taxonomy.

## Memory

Memory is cumulative while execution is reversible: facts, decisions, failure lessons and user
instructions live in SQLite and survive rollbacks and runs. Immutable failure lessons are surfaced
first. See [Memory and Context](Memory-and-Context).

## Worktree isolation

One worktree per run, under the state directory, on branch `gcae/<run-id>`. Your working tree is not
the workshop; it changes only when a verified run merges, and `gcae undo` reverses that.

## Intervention

You can pause, stop, or inject an instruction while a run is going. An instruction is immutable
memory: it discards speculative work, replans and re-arms a run that was waiting for you.
