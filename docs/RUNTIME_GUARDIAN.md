# Runtime Guardian

The Guardian (`src/gcae/guardian.py`) is deterministic health supervision for the agent
loop. It is **not** an agent: it solves no user task, plans nothing, writes no code and
makes no product decisions. It answers one question — *did the machinery behave normally,
and if not, which bounded recovery applies?* — and it answers without an LLM.

## Separation from the evaluator

| | Guardian | Evaluator |
|---|---|---|
| Asks | did the mechanism behave normally? | did the result advance the objective? |
| Sees | exit codes, timeouts, schemas, heartbeats | goals, diffs, evidence, criteria |
| On `python game.py` exit 0 | OK | may still FAIL (restart still broken) |
| On empty model output | `EMPTY_RESPONSE` → retry/fallback | never sees it (Guardian handles first) |

The Guardian decides recoveries; `Runtime` executes them (repo, memory and provider access
stay in the runtime). A broken controller cannot break the check that catches it, and a
Guardian crash stops the run safely with the evidence persisted (`_guardian_crash`).

## Checkpoints

- **Every model invocation**: `pre_model_check` (state valid, context within budget,
  provider/schema present) → call → `post_model_check` (empty, truncated, schema-invalid,
  stall, timeout, rate-limit, context overflow, network). Invalid output never reaches
  the controller as a decision.
- **Every tool operation**: `pre_tool_check` (worktree exists, path inside workspace,
  command allowed) → execute → `post_tool_check` (orphans, worktree sanity, evidence
  presence — even when the exit code is 0).
- **Every step boundary**: `step_check` (state serializable + persisted, accepted commit
  present, worktree sane, memory responsive, evidence linked, trajectory consistent, no
  orphans or stale sessions). Violations recover before the step runs.
- **Every recovery**: `verify_recovery` re-checks the repaired state. A failed repair
  blocks instead of looping.

## Bounds and escalation

`Guardian.allow(action, key, limit)` budgets each recovery type; `record_success` clears
a budget once the underlying problem is fixed. Typical ladder for a dead provider: retry,
retry with backoff, configured fallback model, then blocked — never infinite.

## Heartbeat and stalls

`Heartbeat` tracks last model/tool/persist/checkpoint times plus in-flight markers, all
in memory. `stall_status` combines signals: **hard** when a model call or process exceeds
the hard budget (mechanism stuck → Guardian recovery), **soft** when the agent is active
but nothing verified for the soft budget (→ diagnose, reconstruct, force strategy change).
One signal alone never decides.

Health is one of six states — `healthy`, `degraded`, `recovering`, `waiting`, `blocked`,
`fatal` — published as `health_changed` events and shown as a single top-bar state with a
compact Health panel; `[h]` shows per-subsystem rows built from live probes.
