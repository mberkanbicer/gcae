# Verification: evidence-backed completion

Final verification does not ask "does this look finished?". Every mandatory success
criterion must map to evidence and reach PASS.

Deterministic criteria (`file exists:` / `file contains:` / `file contains exactly:` /
`command succeeds:`) are checked by the runtime and their verdicts are recorded in the
ledger automatically.

Non-deterministic criteria (with `[verifier] kind = "hybrid"`) are decided by the ledger
first, the model judge second:

1. no ledger evidence speaks to the criterion → **INSUFFICIENT_EVIDENCE** — the task is not
   complete, and the run replans with the missing evidence named;
2. contradicting evidence without support → **FAIL** (the contradiction is cited as
   `E<id> (summary)`);
3. otherwise the judge rules over the cited evidence bundle (supporting and contradicting),
   fails closed on provider error, and must cite concrete observations to pass.

Each `CriterionResult` carries `status` (`pass` / `fail` / `insufficient`) and the
`evidence_ids` it rests on; completion requires PASS for every criterion plus workspace
hygiene. The runtime then records one ledger entry per criterion verdict, so completion is
itself evidence-backed — and `tests/test_hardening.py` proves that two supported criteria
plus one unsupported criterion refuse completion.
