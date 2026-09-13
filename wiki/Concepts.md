# Concepts

## Semantic step

The unit of progress. A step has a goal, a rationale, an expected result, an intended scope and
validation requirements. Tools run freely *inside* a step — reading five files is one step, not five
units of progress. A step ends when the agent declares it complete, and then it is validated and
evaluated.

## Trusted state and speculative state

- **Trusted state** is the last accepted checkpoint: a commit on the run branch that passed
  deterministic validation and the evaluation that followed it.
- **Speculative state** is everything the agent changed in its worktree since that checkpoint.

Rejection discards the speculative state (`reset --hard` plus cleanup, inside the worktree only) and
never touches the trusted commits.

## Run phases

```
ANALYZE → PLAN → EXECUTE → VALIDATE → EVALUATE → CHECKPOINT | ROLLBACK → VERIFY → COMPLETE
```

`FAILED` is reachable from every phase, and every failure keeps the accepted commits. Phase changes
are emitted as events, so the dashboard and the JSONL log always agree on where the run is.

## Validation, evaluation, verification

Three different questions, asked in that order:

1. **Validation** is deterministic and cheap: your configured commands, `git diff --check`, scope
   violations, dependency-manifest changes, workspace hygiene. It produces evidence, not opinions.
2. **Evaluation** is a structured model decision over that evidence: `accept`, `rollback`, `replan`,
   `continue` or `finish_candidate`, with a reason, a progress score and memories to promote.
3. **Verification** is the final gate: every success criterion is checked again on the finished tree,
   plus a hygiene check, before the run may complete.

## Success criteria

Checkable statements, either supplied with `--criterion` or derived by the planner from your request.
The deterministic forms are listed in the [Quickstart](Quickstart). Unsupported criteria fail closed
unless `[verifier] kind = "hybrid"` enables a model judge, which must cite evidence and fails closed
on provider errors or empty evidence.

## Memory

Facts, decisions, observations, failure lessons and immutable user instructions live in SQLite and
are retrieved by relevance (FTS5). Memory is cumulative: a rollback removes code, never knowledge.
That is what makes a replanned step different from the attempt that just failed.

## Worktree isolation

One run, one branch (`gcae/<run-id>`), one worktree under the state directory. Your checkout is not
the workshop; it receives the result only through the recorded, reversible merge.

## Intervention

- **Pause / resume** — pause before the next model or tool action.
- **Stop** — end the run at an iteration boundary; state is persisted and resumable.
- **Instruction** — inject a constraint or correction; the runtime stores it as immutable memory,
  discards speculative work and replans.
- **Question** — when the agent cannot proceed it asks (`waiting_for_user`) instead of guessing.
