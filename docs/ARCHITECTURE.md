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
| `safeguards.py` | repetition guard, stagnation window, hygiene check |
| `recovery.py` | self-recovery: trace assembly from the run's own record, advisor prompt, `Diagnosis` |
| `providers.py`, `http_provider.py` | structured-output providers, streaming progress, stall detection |
| `cli.py` | run/resume/list/inspect/merge/undo, headless and TUI modes |
| `tui/` | Textual dashboard: presentation reducer, widgets, viewers, dialogs (see `docs/TUI.md`) |

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
by `max_steps`. Repeated identical tool calls within a step force evaluation of the current
candidate (accepted when valid, rolled back by the evaluator otherwise) instead of destroying work
that may already be correct.

## What counts as evidence

Deterministic validation decides pass/fail on exactly three things: configured commands, `git diff
--check`, and unresolved merge conflicts. Everything else it collects — scope, dependency manifests,
file counts, deletions — is *evidence*: it is reported to the evaluator, shown in the dashboard and
kept in the event log, but it never fails a step by itself.

That distinction is deliberate. A plan's `intended_scope` is a guess, and treating a guess as a hard
gate makes a run impossible whenever the plan is wrong (observed: a planner writing prose into the
scope field rolled back every attempt to create the file the task asked for). The evaluator and the
final verification gate are what decide whether work is good, and both see the evidence.

The candidate diff the agent works from is the working tree **against the last checkpoint**, so
untracked and staged changes are visible: an agent that cannot see the file it just wrote rewrites it
forever (observed: six rewrites of one complete script).

## Replanning and safeguards

Replanning discards speculative changes, keeps accepted commits and knowledge, and preserves
completed steps. Identical tool calls are blocked after `repetition_limit`; repeated
non-progressing iterations trip `stagnation_window`; two consecutive rejected steps escalate
controller decisions to `models.escalation` when configured.

## Liveness

The loop is synchronous, so a model call could otherwise look like a dead process. Two mechanisms
keep it observable:

1. **Progress events** — every provider call runs inside `Runtime._progress(role, provider)`, which
   emits `provider_started`, forwards streamed updates as rate-limited `provider_progress` events and
   closes with `provider_finished` (elapsed + characters).
2. **Heartbeats** — a daemon thread emits `provider_waiting` every `PROVIDER_HEARTBEAT_SECONDS` while
   the call is silent, for any provider, streaming or not.

A provider that stops producing data is a *detected failure*: `[provider] stall_timeout` bounds each
read, the error names the silence and the characters already received, and it enters the recovery
ladder like any other fatal condition. Nothing in the loop waits unboundedly.

## Failure taxonomy

Self-recovery is the runtime's *default* response to failure, not a feature of the stagnation
path. Every condition that can stop a run is classified, and all but the last one are handled
without the user:

| Condition | Response |
| --- | --- |
| tool errors, failed validation commands, scope warnings | evidence for the evaluator; a step is rejected and replanned, the run continues |
| rejected step, non-productive attempts, step budget exhausted | recovery ladder: change hypothesis → escalate → **diagnose** → ask → fail |
| unusable provider output, stall, evaluator output, unexpected exception | same ladder: the advisor reads the trace |
| transient network failure (429, 5xx, dropped connection) | retried with exponential backoff and `Retry-After` support before it is even a failure |
| a role's model is down or misconfigured | **failover**: the role moves to `models.escalation` once and the run continues on it (`model_failover`) |
| planner outage (with user criteria) | deterministic planner takes over, `planner_fallback` event |
| planner outage (no criteria) | the advisor may supply the criteria and a first step (`success_criteria_adopted`) |
| merge conflict | handed to the agent inside the run worktree; re-verified; branch kept if it cannot be resolved |
| memory or event log failure | **degraded mode**: the run continues, the loss is recorded (`runtime_degraded`, `state.degradations`, CLI summary) |
| state file failure | the run **stops** (`failed: run state could not be written…`): a stale `state.json` would make `gcae resume` continue from a checkpoint the branch has moved past |
| the same rejection three times | escalate the controller to `models.escalation` before asking the user (`model_failover`) |
| two runs on one repository | refused up front by the per-repository lock (`RunLock`), which covers run, resume, merge and undo |
| recovery itself failing | contained: recorded as a failure memory, the caller's ladder continues |
| user asked and nothing changed, or the advisor says stop | run fails, accepted checkpoints are still delivered |

Recovery itself is not the only automatic response: a *broken model* is a different class of
failure, because diagnosing it would require calling the same endpoint. That is what
`models.escalation` is for (see `docs/PROVIDERS.md`).

Two rules make the ladder trustworthy: it is **bounded** (`runtime.recovery_attempts`, and each
successful correction buys `runtime.recovery_budget` iterations, never unlimited), and it never
replaces evidence — a correction is an ordinary semantic step that still has to pass validation,
evaluation and the final verification gate.

Degraded mode is deliberately narrow: it covers bookkeeping (state, memory, events), never
correctness. To make that safe, recovery tolerates all three: a diagnosis works from whatever
subsystem is still alive.

## Self-recovery

When a run is about to give up, the loop leaves the model-driven path and asks the recovery advisor
instead:

```
stagnation | provider failure | evaluator failure | budget exhausted
        │
        ▼
  build_trace(state, events, memories, candidate)   ← the run's own record, not its context
        │
        ▼
  Diagnosis{root_cause, corrective_instruction, strategy}
        │
   replan ──► corrective step queued + extra iterations ──► back to EXECUTE
   ask_user ──► waiting_for_user (question = root cause + suggested correction)
   stop ──────► failed: recovery advised stopping: <root cause>
```

The advisor is an independent reading of the same run: it sees what was tried — filtered events,
failure memories, validation and verification evidence, the candidate diff — rather than what the
controller currently believes, which is exactly the perspective a stuck loop lacks. A recovery result
is an ordinary semantic step: validated, evaluated and checkpointed like any other, and still subject
to the final verification gate. Self-diagnoses are bounded by `runtime.recovery_attempts` and each
successful correction grants `runtime.recovery_budget` extra iterations; a diagnosis that cannot be
parsed leaves the ladder (ask the user, then fail) intact.

## Final verification and completion

`finish_candidate` enters a separate verifier that checks every success criterion and runs a
hygiene pass. Checkable criteria (`file exists`, `file contains`, `command succeeds`) are verified
deterministically. With `verifier.kind = "hybrid"` the configured verifier model may judge criteria
that have no deterministic form, using the objective, changed files, validation evidence, a
truncated diff and bounded worktree samples; it must return structured evidence, and any error,
empty evidence or negative verdict fails verification. The default (`deterministic`) fails
unsupported criteria closed. If verification passes while the worktree still holds uncommitted
changes, the runtime commits `gcae: verified final state` first, so `accepted_commit` always equals
the verified tree.

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
