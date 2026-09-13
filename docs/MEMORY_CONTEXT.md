# Memory and context

Memory is append-oriented SQLite with an FTS5 index stored once per runtime directory
(`<state>/memory.db`) and shared by all runs. Records carry type, provenance, commit, importance,
and immutability. Immutable failures and user instructions cannot be edited and are retained after
rollback. `update` refreshes the FTS index through an `AFTER UPDATE` trigger, so retrieval never
returns stale content.

Pinned records are scoped to the current run; retrieval (`search`) is global, so relevant facts
and failure lessons from earlier runs surface in later runs without being pinned.

`ContextBuilder` reconstructs a fresh prompt from pinned request/constraints/criteria/current
goal/accepted commit and relevant FTS records; optional records are dropped first under the token
budget. `estimate_tokens` uses a conservative `(len + 3) // 4` mixed English/code approximation;
the budget comes from `provider.context_limit`.

Pinned material is never truncated. If it alone exceeds the configured budget, the context is
allowed to exceed that budget rather than losing the original request, constraints, success
criteria, current goal, accepted commit, or critical failure lessons. Current diffs, active files,
the latest persisted validation result, and recent observations are rebuilt on every call. No
conversation history is retained and no recursive summarization is performed.
