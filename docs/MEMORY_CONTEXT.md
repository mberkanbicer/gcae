# Memory and context

## Memory

`memory.db` lives at the runtime-state root and is shared by all runs. Records are typed
(`user_instruction`, `fact`, `decision`, `failure`, `observation`, `artifact`) and carry provenance:
`id`, `run_id`, `step_id`, `source`, `commit_sha`, `created_at`, `importance`, `immutable`.

- Immutable records (original request, constraints, success criteria, user overrides) cannot be
  edited; `MemoryStore.update` refuses them.
- Retrieval uses SQLite FTS5 with punctuation-safe tokenization. No embeddings, no vector store.
- Rollback never deletes knowledge: failure lessons from rejected trajectories stay and are
  retrieved by later runs, so the same failed strategy is not repeated without new evidence.
- Evaluators may promote `memories_to_promote`; the runtime owns their provenance fields.

## Working memory

Each active step has a small `WorkingMemory`: hypotheses, active files, current blocker, latest
findings and pending validations. It is rendered into the controller context, updated from tool
results, and reset when the step is settled. Durable knowledge goes to SQLite; transient tool
events do not stay in the working set.

## Context reconstruction

Context is rebuilt for every model call from persistent state, never accumulated as conversation
history and never recursively summarized:

```
objective · original request · hard constraints · success criteria
latest user instruction · accepted commit · current goal/budget
plan · relevant memory (FTS) · pinned memory
working memory · active files · current diff · latest validation · observations
```

Pinned entries are the objective, original request, constraints, success criteria, latest user
instruction, accepted commit, current goal and critical failure lessons. They are never truncated.
Under budget pressure, optional entries are dropped first (verbose command logs, old low-value
observations, unrelated plan detail, lower-importance memory).

## Budgeting

`provider.context_limit` is the token budget; tokens are estimated conservatively as
`(characters + 3) // 4`. If pinned content alone exceeds the budget, the context exceeds the budget
rather than losing the source of truth. The runtime records the size, pinned count and omitted
count of the last context for the TUI.

## Events

`runs/<run-id>/events.jsonl` is append-only and holds the full lifecycle (run start, decisions,
tool results, validation, evaluation, checkpoint, rollback, replan, user override, verification,
completion/failure). It is for audit, resume and the TUI; the entire log is never sent to the
model.
