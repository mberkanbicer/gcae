# Architecture

One process drives a single agent through semantic steps. Git checkpoints are the trusted state;
SQLite memory is cumulative knowledge that survives every rollback.

## Modules

| Module | Responsibility |
| --- | --- |
| `runtime.py` | the loop: phases, budgets, checkpoints, rollback, recovery, repository lock |
| `state_machine.py` | legal phase transitions |
| `controller.py` | one decision per call: a tool call or a step completion |
| `planner.py` | initial plan, replanning, the deterministic fallback planner |
| `tools.py` | worktree-confined tools (`read_file`, `write_file`, `create_file`, `apply_patch`, `list_files`, `run_command`) |
| `validation.py` | deterministic validation: commands, `git diff --check`, scope evidence |
| `evaluator.py` | deterministic or model judgement of progress |
| `verifier.py` | final criteria gate, optional hybrid model judge |
| `recovery.py` | the self-diagnosis advisor: reads the run's trace, returns a `Diagnosis` |
| `memory.py` | SQLite FTS5 memory and the append-only event log |
| `context.py` | per-call context reconstruction and token budgeting |
| `git.py` | worktree, branch, checkpoint, merge, conflict, undo primitives |
| `providers.py`, `http_provider.py` | provider protocol, streaming, retries, stall detection |
| `persistence.py` | `state.json` schema and corruption handling |
| `cli.py`, `tui/` | entry points; the engine never imports Textual |

## Data flow of one iteration

```
state + memory + plan        →  context (budgeted, pinned data kept)
context                      →  controller  →  tool call or completion
tool call                    →  worktree (observed, recorded)
completion                   →  deterministic validation (commands, diff check, scope)
validation + candidate diff  →  evaluator   →  accept | rollback | replan
accept                       →  commit "gcae: <goal>", advance the plan
rollback                     →  reset --hard inside the worktree, record the lesson, replan
plan exhausted               →  final verification → merge → complete
```

## Failure taxonomy

Self-recovery is the *default* answer to failure, not a special case:

| Condition | Response |
| --- | --- |
| failed validation command, scope warning | evidence for the evaluator; the step is rejected and replanned |
| rejected step, non-productive attempts, exhausted budget | recovery ladder: change hypothesis → escalate → diagnose → ask → fail |
| unusable provider output, stall, evaluator output, unexpected exception | same ladder, on the run's own trace |
| transient network failure (429, 5xx, timeout, dropped connection) | retried with exponential backoff and `Retry-After` first |
| a role's model is dead or misconfigured | failover to `models.escalation` once (`model_failover`) |
| the same rejection three times | escalate the controller before asking the user |
| planner outage | deterministic planner with user criteria; otherwise the advisor may supply the criteria |
| merge conflict | handed to the agent inside the run worktree, re-verified, retried |
| memory or event log failure | **degraded mode**: the run continues, the loss is recorded |
| state file failure | the run **stops** (`run state could not be written`) so `resume` cannot lie |
| recovery itself failing | contained; recorded as a failure lesson, the ladder continues |
| branch cannot be merged | reported; the branch stays for `gcae merge` |

Two rules make the ladder trustworthy: it is **bounded** (`recovery_attempts`, `recovery_budget`),
and it never replaces evidence — a correction is an ordinary semantic step.

## Two kinds of state

| | Trusted | Cumulative |
| --- | --- | --- |
| Carrier | Git commits on `gcae/<run-id>` | `memory.db`, `events.jsonl` |
| Survives rollback | yes (it *is* the rollback target) | yes, deliberately |
| Losing it | unacceptable — a checkpoint must be a verified tree | survivable: the loss is recorded as a degradation |

`state.json` is the exception on the "bookkeeping" side: it is not allowed to degrade, because a stale
state file still looks resumable.

## Event model

`events.jsonl` is append-only (thread-safe) and is the run's trace: provider calls, decisions, tool
results, validation evidence, evaluation, rollback, recovery, failover, degradations, merges. The
dashboard reduces those events into display state; `gcae inspect` prints the durable ones. The trace
is exactly what the recovery advisor reads when a run cannot continue.

## Invariants

1. Execution is reversible; knowledge is cumulative.
2. Exactly one worktree per run, under the runtime directory; the user's tree is never the workshop.
3. The loop owns every Git operation; the model never runs `git`.
4. Every failure is diagnosed before it can end a run.
5. Only acceptance creates commits.
6. Context is reconstructed per call; pinned data cannot be budgeted away.
7. Structured decisions only — invalid model output is repaired or fails the run, never guessed.
8. The TUI is first-class and optional: the engine runs headless.
