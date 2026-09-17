# Failure interpretation and recovery

Failure is information. A failed execution is classified before it is reacted to
(`INTERPRET FAILURE BEFORE REACTING`): exit code, output, timeout kind and interactivity
become a `FailureKind` (`code_error`, `test_failure`, `command_timeout`,
`interactive_input_required`, `dependency_missing`, …) plus a lesson, an evidence record,
and — on first sight of a new failure signature — verified progress, because learning is
progress.

The recovery ladder is bounded and evidence-chosen, not mechanical:

1. reinspect immediate evidence (the failure lesson is injected into the next decision)
2. try a materially different approach (the strategy ledger refuses an unchanged repeat
   while the candidate tree is unchanged)
3. repair the candidate in place (evaluator `repair`: direction valid, implementation wrong)
4. replan (evaluator `replan` or verification failure names what is missing)
5. escalate to the configured stronger model (`[models.escalation]`, once per role)
6. self-diagnose from the persisted trace (`recovery.py`, bounded by `recovery_attempts`)
7. ask the user only when externally blocked (`waiting_for_user` / `blocked`)

### When the method itself is exhausted

Per-approach fingerprints (tool plus arguments) and the unchanged-tree rule can be evaded
by cosmetic changes — a different flag, a reworded command, a trivial edit between
attempts. So the runtime also tracks failures at the *failure level*: how often the same
normalized error signature has returned, no matter which command produced it
(`failure_repeat_limit`, default 3). When the limit is crossed:

- the method is declared exhausted (`method_exhausted` event, an immutable failure memory);
- the decision prompt pins a **mandatory method change** directive naming the error, the
  commands that produced it and the lesson — new information first (read the failing code,
  write a minimal reproduction), then a different strategy, or `replan`;
- an exact repeat of any command that produced the exhausted failure is refused on sight;
- a repeated failure signature no longer counts as knowledge progress — repeats fill the
  stagnation window instead, so a perturbation loop reaches the recovery ladder instead of
  silently burning the iteration budget.

A recovery replan clears the exhausted-method list (the advisor prescribed a different
method, and a fix changes the failure) while keeping the recurrence counts: if the same
failure comes back after the replan, the method re-exhausts on its first recurrence.

`BLOCKED` is not `FAILED`: blocked records the last trusted state, the exact blocker, the
attempted strategies, and what would unblock; failed means the runtime itself cannot
continue safely (or the user was already asked).

## Where the Guardian fits

The **Runtime Guardian** (`docs/RUNTIME_GUARDIAN.md`) wraps this ladder's inputs: every
model call gets pre/during/post checks (empty, truncated, invalid-schema, stalled and
rate-limited outputs are classified before the evaluator ever sees them), every tool
operation gets a post-check even on success (exit code, timeout, orphan process, worktree
state), and every step boundary verifies persistence and integrity invariants. A failed
check emits `guardian_check` with a recovery action; the runtime executes it, verifies the
recovery itself, and only resumes if the post-recovery health check passes. Recovery
budgets are bounded per failure kind — same provider retry, backoff, then fallback, then
block; never an endless loop, and never a second LLM agent. Soft stalls (active, no
verified progress) route to the ladder above; hard stalls (mechanism stuck) route to
Guardian recovery. If the Guardian itself throws, the run stops safely with FATAL and the
trusted checkpoint survives. Health states (`healthy`, `degraded`, `recovering`,
`waiting`, `blocked`, `fatal`) surface on the dashboard and `[h]`.

## Crash windows (see STATE_MACHINE for the table)

A *process death* is not a run failure — it is a resume with reconciliation: unrecorded
checkpoints are discarded and named, branch divergence is restored forward and reported,
the plan is repaired to the boundary git can prove, torn event tails are truncated, and
merge intent persisted before `git merge` guarantees the undo record exists even when
the process died mid-merge. Every repair emits a `resume_reconciled`/plan event; nothing
is silently healed. `tests/test_resume_recovery.py` proves each window with file-level
crash states plus one real SIGKILL run resumed in a second process.
