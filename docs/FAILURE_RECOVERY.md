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

`BLOCKED` is not `FAILED`: blocked records the last trusted state, the exact blocker, the
attempted strategies, and what would unblock; failed means the runtime itself cannot
continue safely (or the user was already asked).
