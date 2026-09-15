# Memory and Context

## Two systems, two purposes

| | Memory (`memory.db`) | Context (per call) |
| --- | --- | --- |
| Nature | cumulative, survives rollbacks and runs | reconstructed for every model call |
| Contains | facts, decisions, failure lessons, user instructions, promoted memories, the evidence ledger | objective, constraints, criteria, goal, accepted commit, relevant memory, recent trace |
| Never | deleted on rollback | allowed to grow into a conversation |

## Memory kinds

| Kind | Written by | Notes |
| --- | --- | --- |
| `fact` (and the evaluator's own kinds) | the agent, promoted through an evaluation | durable knowledge; the kind string comes from the evaluation, so it stays open |
| `decision` | the runtime and the agent | why an approach was chosen or abandoned |
| `failure` | validation, evaluation, recovery, crashes | **immutable**: lessons are never overwritten |
| `observation` | the runtime | what a step saw, kept for the next attempt |
| `user_instruction` | you | immutable: "do not touch the database" survives every replanning |
| stagnation / replan markers | the runtime (as `decision`) | the change-hypothesis record the ladder writes before trying something else |

SQLite FTS5 retrieval ranks by relevance to the current step, scoped to the source repository:
knowledge accumulates across runs of one project without leaking into another's. Rows written
before scoped retrieval existed carry no repository; opening a run backfills them from the
persisted run states where the repository is still known. Immutable
lessons are surfaced first, because a repeated mistake is more expensive than a missing fact.

## Evidence ledger

One record per command result, validation, interactive session and criterion verdict, each with
`supports` / `contradicts` claims. Evidence is present, absent, supporting or contradicting —
never scored. The verifier maps every success criterion to its evidence (PASS needs support, no
evidence is INSUFFICIENT, contradiction is FAIL), and the dashboard shows running counts.

## Retrieval and budgeting

The context builder assembles a prompt and fits it into `provider.context_limit`:

1. pinned data first — request, hard constraints, success criteria, current goal, expectation,
   accepted commit, failure lessons (the last 8; the store keeps all of them retrievable);
   P0 is never dropped;
2. then memory and recent observations, ranked and truncated;
3. then the candidate diff and tool results, trimmed to the budget.

If the budget cannot hold the pinned part, the pinned set is returned as-is: shortening the
projection never destroys P0.

## Inspecting the real payload

The dashboard's context screen (`c`) shows the exact prompt the model receives, section by section,
with the estimated token count — the fastest way to understand a surprising decision. `gcae inspect`
shows what the run kept as durable knowledge.

## Working memory

Between checkpoints the run keeps hypotheses, the current step's observations and tool results. On
rejection the speculative working memory is reset while durable memory stays: that is the whole point
— the next attempt starts smarter than the failed one, with the failure lesson in hand.

## Degraded memory

If `memory.db` cannot be written (for example a locked database), the run does **not** stop:
knowledge for that run is lost, the loss is recorded (`runtime_degraded` event, a bounded
`degradations` list, a WARNING, and a `degraded:` line in the CLI summary) and the work continues.
State is different: a state file that cannot be written stops the run, because a stale state file
still looks resumable. See [Architecture](Architecture).
