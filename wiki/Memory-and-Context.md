# Memory and Context

GCAE keeps two different kinds of state, and confusing them is the most common way to misread a run.

| | Execution state | Knowledge state |
| --- | --- | --- |
| Where | Git commits on `gcae/<run-id>` | `memory.db` (SQLite + FTS5) |
| Lifetime | reversible; discarded on rollback | cumulative; survives rollback, resume and new runs |
| Used by | Git, verification, merge | the context builder for every model call |

## Memory kinds

| Kind | Written by | Meaning |
| --- | --- | --- |
| `user_instruction` | run start, instructions, criteria, constraints | immutable; always pinned into context |
| `failure` | rollbacks, provider failures, verification failures | immutable lesson from something that did not work |
| `decision` | accepted steps, replans, stagnation | a choice worth remembering |
| `observation` | tool results | what a tool returned, trimmed to a summary |
| `fact` | promoted by the evaluator | a durable truth about the project |
| `artifact` | tool results that wrote files | where a large output was stored |

Immutable records (`failure`, `user_instruction`) are pinned and can never be trimmed out of context
— that is what makes "rollback without amnesia" real.

## Retrieval and budgeting

The context builder assembles, in order: pinned instructions and lessons, current state (objective,
criteria, plan, working memory, step budget), the current diff, validation evidence, recent
observations, and FTS5 hits relevant to the objective. It then fills the configured
`context_limit` by priority and reports what it kept:

```json
{"characters": 31200, "estimated_tokens": 7800, "pinned": 6, "omitted": 1}
```

Omitted records stay in the database and remain retrievable. Token counts are estimates
(`chars / 4`) and are labelled `(est)` wherever they appear.

## Inspecting the real payload

- Dashboard: `c` opens the context inspector — the sections actually sent, their sizes, and each
  section's content.
- Dashboard: `m` opens the memory inspector — records grouped by kind with step, commit, timestamp
  and source provenance.
- CLI: `gcae inspect <run-id>` shows persisted state; the raw context of any iteration is in
  `runs/<run-id>/events.jsonl` and the tool results are in `runs/<run-id>/tool-results/`.

Large tool outputs are externalized to `runs/<run-id>/tool-results/` and referenced by summary plus
artifact path, so one huge `pytest` run cannot consume the whole budget.

## Working memory

Alongside durable memory, each step carries a small working set — active files, the current blocker,
hypotheses, findings and pending validations. It is settled into durable memory when a step is
accepted and cleared when a step is rejected, which keeps the model focused on the current attempt.
