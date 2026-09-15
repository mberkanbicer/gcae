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

When the budget cannot fit the pinned set, the pinned set is returned as-is: shortening the
projection never destroys what the user said.
