# Evidence ledger

A small, typed, append-only ledger — deliberately not a knowledge graph, not a vector
database, and not scored. Evidence is present, absent, supporting, or contradicting.

`EvidenceRecord` (`src/gcae/models.py`) → `evidence` table (`src/gcae/memory.py`):

```
id · run_id · trajectory_step_id · kind · claim_or_subject
source_type · source_reference · summary · supports[] · contradicts[] · created_at
```

Kinds: `COMMAND_RESULT`, `TEST_RESULT`, `BUILD_RESULT`, `FILE_STATE`, `GIT_DIFF`,
`STATIC_CHECK`, `INTERACTIVE_SESSION`, `ARTIFACT`, `USER_CONFIRMATION`, `OBSERVATION`.

Who writes what:

- every executed command (success → supports the step's expectation; failure → contradicts it)
- a failure signal observed in output → contradicts, even when the exit code says success
- a process asking for input → `INTERACTIVE_SESSION`
- deterministic validation → `GIT_DIFF` (pass/fail) + one record per validation command
- final verification → one record per criterion verdict (`supports` on pass, `contradicts` on fail)

Consumers:

- **The verifier** builds the criterion → evidence mapping: supporting and contradicting
  records per criterion decide PASS / FAIL / INSUFFICIENT_EVIDENCE; the model judge is only
  consulted when the ledger has evidence and rules over the cited bundle.
- **The TUI** shows running counts (`3 supporting · 1 contradicting · 9 records`) in the
  validation panel and puts contradictory evidence on the semantic timeline.
- **The trajectory** carries `evidence_ids` per attempt, so an accepted step always names
  what proved it (invariant B).

No confidence numbers: a record is evidence or it is not; relevance and contradiction are
judged, not computed.
