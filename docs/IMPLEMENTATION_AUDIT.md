# Implementation audit

Factual classification of the shipped code against GCAE's intended identity, performed before
the 0.4.0 hardening. Components are KEEP / HARDEN / REPAIR / SIMPLIFY / REMOVE / MISSING, and
each entry names what changed (if anything) in 0.4.0.

## Current architecture (as shipped)

One process, one agent, one state: `Runtime` (`src/gcae/runtime.py`) drives a single loop —
choose a semantic step, execute tools inside one isolated git worktree, validate
deterministically, evaluate, checkpoint or rollback, verify against success criteria, merge —
with an advisor (`recovery.py`) that diagnoses the run's own persisted trace when the ladder
runs out, and a SQLite/FTS5 store (`memory.py`) that keeps knowledge across rollbacks and
runs. Roles (controller, planner, evaluator, verifier, recovery) are model invocations inside
that one runtime, optionally routed to different models (`[models.*]`), never separate agents.

```
RECONSTRUCT → ACT → OBSERVE → JUDGE → COMMIT OR REVERT → LEARN → (loop)
```

## Classification

| Component | Verdict | Notes / 0.4.0 change |
| --- | --- | --- |
| Runtime loop (`runtime.py`) | KEEP, HARDEN | trajectory lifecycle, evidence recording, repair decision, verified-progress stagnation added |
| Git/worktree layer (`git.py`) | KEEP | one worktree per run; loop owns all git; verified by tests |
| Checkpoint semantics | KEEP | accepted commit = trusted state; candidate = speculative; TUI words it that way |
| Command execution (`execution.py`) | KEEP | batch / scripted / PTY, typed timeouts, prompt detection, process-group kill |
| Failure taxonomy + lessons | KEEP, HARDEN | unchanged; now also recorded as evidence |
| Strategy fingerprinting | KEEP | tool+args fingerprint, tree-aware refusal |
| Context reconstruction (`context.py`) | HARDEN | pinned failures capped (last 8); retrieval scoped to the source repository; header duplication removed; token estimate corrected |
| Memory store (`memory.py`) | HARDEN | `source_repo` column (migrated); evidence ledger table added |
| Planner | KEEP, HARDEN | plan steps now carry `expected_evidence` and `failure_signals` |
| Evaluator | HARDEN | `repair` decision added; promotion capped at 3 per step |
| Verifier | HARDEN | criterion → evidence mapping; PASS / FAIL / INSUFFICIENT_EVIDENCE; contradiction blocks |
| TUI | KEEP, HARDEN | trajectory verdicts in the timeline; evidence counts in the validation panel |
| CLI (`prune`, `input`, …) | KEEP | unchanged |
| Persistence (`persistence.py`) | KEEP | state.json stays the execution state; knowledge lives in memory.db |

## Architectural gaps found and fixed in 0.4.0

1. **TrajectoryStep was implicit.** The semantic attempt existed only as a mutable PlanStep;
   the persisted record could not answer "what was expected, what happened, why accepted".
   Now `TrajectoryStep` is a first-class typed record persisted in `state.json` (bounded) with
   expectation, failure signals, actions, observations, evidence ids, verdict and knowledge.
2. **Execution state and knowledge state were separated only by convention.** Rollback
   preserved memory, but nothing made the split typed or tested. The evidence ledger and the
   invariant tests (A–J, `tests/test_hardening.py`) now pin it down: rollback resets the
   worktree and accepted commit; failures, lessons and evidence records survive.
3. **Evidence was scattered in event payloads.** There was no place a criterion could point
   at. The evidence ledger (`evidence` table) now holds one record per command result,
   validation, interactive session, and criterion verdict, with `supports` / `contradicts`.
4. **Completion could rest on an ungrounded claim.** Non-deterministic criteria were judged
   from samples alone. Now: no ledger evidence → INSUFFICIENT_EVIDENCE (not complete);
   contradiction without support → FAIL; the judge only rules over cited evidence.
5. **Cross-project memory contamination.** FTS retrieval ignored the repository. Knowledge is
   cumulative by design, but a lesson learned in project A must not enter project B's
   decision context; retrieval is now scoped to `source_repo`.
6. **Pinned context grew without bound.** Every failure was pinned forever; the pinned set is
   now bounded (last 8 failures) while the store keeps everything retrievable.

## Redundant or generic-agent behavior

None found beyond what the audit above repaired: there is no chat transcript, no message
history, no recursive summarization, no confidence scores, no multi-agent framework, and the
dependency set is exactly `pydantic`, `httpx`, `textual`. Removed in this pass: dead helper
`looks_interactive`, duplicated pinned user-instruction records, the chars/4 token estimate.

## Still missing (accepted, not silently ignored)

- Run-data pruning has a retention TTL (`--older-than`, `[runtime] run_retention_days`);
  pruning removes run directories only — `memory.db` stays cumulative by design.
- The evidence ledger is append-only; no compaction (run history is the archive).
- The judge cannot *request* new evidence at verification time; the run's ladder does that
  work instead (verification failure replans with the missing evidence named).
- The judge weighs recency explicitly (newest-first bundle with timestamps), not a score.
- Replan reasons are recorded free text, not a typed category set
  (`USER_OVERRIDE`, `INVALID_ASSUMPTION`, …). They are always present and persisted with
  the `PlanVersion`, but nothing enforces the vocabulary. Add when reason statistics or
  replan-policy decisions actually need the categories.
- There is no revalidation model: `verified_criteria` drives replan coverage checks, but a
  criterion affected by later changes has no `REVALIDATION_REQUIRED` state and there is no
  impact analysis distinguishing "revalidate the assumption" from "redo the work". Add
  when verified criteria start being invalidated by cross-step changes in practice.
- Timeline events are not individually expandable: Level 2 detail lives in the dedicated
  screens (`Enter`, `d`, `e`, `t`, `h`, `m`, `c`), not per-event expansion on the timeline
  itself. Add when a failure line is not enough to reconstruct command/output/evidence
  without opening those screens.
- The ACTIVE panel has no in-panel model row; model, role and elapsed time live in the
  status bar (`OPENROUTER · model · role · 12s`) and the phase label in ACTIVE. The five
  core questions stay answered; only the spec's example layout differs.
