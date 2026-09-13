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

1. store an immutable decision memory that the previous approach is exhausted, so the next decision
   must change hypothesis;
2. escalate to the configured stronger model (`[models.escalation]`) once per session;
3. ask the user: the run enters `waiting_for_user` with a question that names the attempts, the
   accepted steps and the latest checkpoint. The plan and every accepted commit are preserved;
4. fail with `execution stagnated` only when the user was already asked in that session and nothing
   changed. Accepted checkpoints are still delivered by the merge step.

A resumed run is a new user intervention, so it asks again instead of dying. Answering an
instruction (`i` in the dashboard, `gcae resume` in the CLI) re-plans from the accepted state.

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
