# Memory and context

Memory is append-oriented SQLite with an FTS5 index. Records carry type, provenance, commit, importance, and immutability. Immutable failures and user instructions cannot be edited and are retained after rollback. `ContextBuilder` reconstructs a fresh prompt from pinned request/constraints/criteria/current goal/accepted commit and relevant FTS records; optional records are dropped first under the character budget.

Pinned material is never truncated. If it alone exceeds the configured character budget, the
context is allowed to exceed that budget rather than losing the original request, constraints,
success criteria, current goal, accepted commit, or critical failure lessons. Current diffs,
active files, validation results, and recent observations are rebuilt on every call.
