# State machine

Phases: `analyze -> plan -> execute -> validate -> evaluate -> checkpoint`, with `rollback`,
`verify`, `complete` and `failed`. Transitions are declared in `state_machine.py`; invalid
transitions raise `ValueError`.

| From | Allowed targets |
| --- | --- |
| analyze | plan, failed |
| plan | execute, rollback, failed |
| execute | execute, plan, validate, verify, rollback, failed |
| validate | evaluate, rollback, failed |
| evaluate | execute, checkpoint, rollback, plan, verify, failed |
| checkpoint | execute, verify, complete, failed |
| rollback | execute, plan, failed |
| verify | complete, checkpoint, plan, failed |
| complete / failed | terminal |

## Stagnation

Stagnation means "this approach is exhausted", not "stop". The detector watches a window of
attempts (default 3) and counts only *non-productive* outcomes: rejected steps and replans. An
accepted step is progress even when it changed no file — the evaluator judged it worthwhile and the
plan advanced.

The response is a ladder:

1. store a decision memory that the previous approach is exhausted, so the next decision must change
   hypothesis;
2. escalate to the configured stronger model (`[models.escalation]`) once per session;
3. **diagnose itself**: the recovery advisor (`[models.recovery]`, default: the controller's model)
   receives a trace built from the run's own persisted record — state, filtered events, validation
   and verification evidence, failure memories, candidate diff — and answers with a structured
   `Diagnosis`: root cause, one corrective instruction, and a strategy. `replan` queues the
   instruction as the next step and grants `runtime.recovery_budget` extra iterations; `ask_user`
   asks directly; `stop` declares the task impossible as stated;
4. ask the user: `waiting_for_user` with a question naming the attempts, the accepted steps and the
   latest checkpoint. The plan and every accepted commit are preserved;
5. fail only when the user was already asked in that session, or when recovery attempts are spent
   and the advisor produced nothing usable. Accepted checkpoints are still delivered by the merge.

Everything is bounded by `runtime.recovery_attempts` (default 2), so self-recovery can never become an
endless loop, and a diagnosis that cannot be parsed leaves the caller's ladder intact.

A resumed run is a new user intervention, so it may ask again instead of dying. Answering an
instruction (`i` in the dashboard, `gcae resume` in the CLI) re-plans from the accepted state.

## Degraded mode

Knowledge and history may be lost; the *record of committed state* may not. A failure to append to
`events.jsonl` or to record memory is reported (`runtime_degraded` event, an entry in
`state.degradations`, a WARNING, and a `degraded:` line in the CLI summary) and the run continues.
A failure to write `state.json` stops the run instead: a stale state file still looks resumable, and
`gcae resume` would then continue from a checkpoint the branch has already moved past. The run fails
with `run state could not be written: …`, the accepted commits stay on the branch, and the event log
still explains what happened. Correctness is never degraded in either case: validation, evaluation
and verification all still have to pass.

## Recovery triggers

Recovery is not only for stagnation. The same advisor reads the trace when a run is about to end
because:

| Trigger | Why it is recoverable |
| --- | --- |
| `step budget exhausted after N iterations` | the advisor can prescribe the missing step; a successful diagnosis grants extra iterations |
| `provider output: …` | repeated unusable provider output may be a prompting/schema problem, not a dead end |
| `evaluator output: …` | same, for the evaluation call |
| `stagnation: …` | repeated non-productive attempts |
| `repeated_failure: the same failure repeated 3 times: …` | identical rejections, even when read-only steps are accepted in between |
| `unexpected error: <Type>: <message>` | an exception inside the loop is a run failure, not a process death: it is recorded as an immutable failure memory, becomes a recovery trigger, and only then ends the run |

The advisor never *replaces* deterministic evidence: a recovery step is a normal semantic step, so it
is validated, evaluated and checkpointed like any other, and the run only completes through the
verification gate.

## Controller actions

- `execute_tool` — run one registered tool; no evaluation yet.
- `complete_semantic_step` — declare the current step finished; run validation and evaluation.
- `replan` — discard speculative work and queue a new step.
- `finish_candidate` — request final verification (only the verifier can complete the run).
- `ask_user` — persist a question and stop; the run is resumable.

## Step lifecycle

`pending -> active -> completed | failed | skipped` (`PlanStep.status`). A step becomes `active`
when its first iteration starts, `completed` when accepted, `failed` on a rejected evaluation, and
`skipped` when superseded by replan or a user override. Evaluation is forced when
`step_tool_calls` reaches `max_tool_calls_per_step`.

## Run status

`running`, `waiting_for_user`, `stopped`, `complete`, `failed: <reason>`. `waiting_for_user` and
`stopped` are resumable: `resume` restores the accepted checkpoint, clears speculative state inside
the worktree, rebuilds context from persistent state, and continues. `complete` and `failed` are
terminal for the automatic loop, but `inspect`, `merge` and `undo` still apply.
