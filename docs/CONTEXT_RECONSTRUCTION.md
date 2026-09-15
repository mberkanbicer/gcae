# Context reconstruction

Context is a temporary projection of authoritative state, rebuilt for every model call and
never accumulated as a conversation, never recursively summarized.

```
persistent state (state.json, memory.db)  →  ContextBuilder  →  role prompt
```

Priority (P0 is lossless, P4 is dropped first):

- **P0** objective, original request, hard constraints, success criteria, latest user
  instruction, accepted commit, current goal, expectation/evidence/failure signals
- **P1** current candidate state (diff, active files), recent execution evidence, critical
  failure lessons
- **P2** relevant decisions, failed strategies, active hypotheses
- **P3** relevant observations, nearby code
- **P4** verbose history (log lines, old observations)

Pinned memory is the lossless layer: user instructions and immutable records always render;
failure records are pinned up to the last 8 (the store keeps all of them retrievable — a cap
on the *projection*, never on the source truth). Retrieval is scoped to the source
repository, so knowledge accumulates across runs of one project without leaking into another.

## Retrieval tiers and gates

Retrieval is tiered, strongest scope first: **current run** → **same project/repository**
→ **explicitly global-reusable records only**. A snake-game run therefore never inherits
Fibonacci decisions or ant-simulator observations (tested in `tests/test_hardening.py`);
legacy rows with no repository attribution stay invisible to scoped searches and are
backfilled best-effort from run state on open. Before anything enters the prompt it must
pass the gates: scope tier, and non-duplication — repeated records are collapsed during
retrieval (`dropped_duplicates`) without deleting the authoritative store. When retrieved
memory occupies more than 40% of the context or the gate is working hard, a single
`context_warning` line (once per run) names the share and the drops; the full numbers stay
in the context screen, which groups retrieved memory by CURRENT RUN / PROJECT / GLOBAL.

When the budget cannot fit the pinned set, the pinned set is returned as-is: shortening the
projection never destroys what the user said.
