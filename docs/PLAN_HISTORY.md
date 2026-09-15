# Plan history and partial replanning

The plan is structured trajectory state, not disposable text. It has three regions with
different stability guarantees:

```
PAST        completed + verified steps — stable, locked, visible
PRESENT     current semantic step — repairable, replaceable
FUTURE      remaining steps — adaptive
```

## Rules

- **Verified past is stable.** A completed, checkpointed step keeps its ID, status, goal
  and checkpoint across replans. Replans only ever touch the current step onward.
- **Identity is stable.** New steps get monotonic IDs (`step-N`); history is never
  renumbered, so old events stay unambiguous. The UI may show positions, but IDs rule.
- **Invalidation needs evidence.** A completed step moves to `invalidated` only with a
  recorded reason and ledger evidence IDs; dependents (via `depends_on`) follow with a
  dependency reason. Anything less is rejected.
- **Invalidation un-verifies what it disproved.** Verified criteria whose supporting
  evidence died with an invalidated step move to `revalidation_required`: not silently
  deleted, not falsely verified. Guardian plan-health treats them as covered (final
  verification re-checks them), the replan prompt asks for a cheap revalidation step
  instead of a rebuild, and a passing final verification clears them.
- **Replans are patches, not rewrites.** `ReplanPatch` carries the base plan version,
  the affected region, invalidations, replacements and new steps. The runtime validates
  deterministically — stale base, unknown IDs, locked rewrites without evidence,
  duplicate IDs, missing rollback targets and dropped success-criteria coverage all
  reject the patch with the active plan untouched.
- **Rollback and plan agree.** Rolling back to an explicit older checkpoint invalidates
  completed steps whose checkpoints are no longer reachable (ancestry-checked), plus
  their dependents. Knowledge — lessons, evidence — never moves. The same ancestry
  reconciliation runs on **resume** when execution sits behind the trusted checkpoint,
  recording the revision under the `resume_reconciliation` reason category.
- **Repair before replan.** Evaluator `repair` keeps the candidate; the strategy ledger
  forces a different method; deterministic partial replan replaces the current step;
  model-backed replan patches happen only after recovery shows local means failed.
- **Versions persist.** Every revision bumps `plan_version` and appends a `PlanVersion`
  (preserved/invalidated/replaced/inserted + reason); the last 20 stay in `state.json`.
  Resume restores versions, history and checkpoint mapping without regenerating anything.

## Step states

`pending → active → completed` (locked, checkpointed), with `failed`/`skipped` for
attempts that never verified, `replaced` (with `replaced_by`) for superseded work, and
`invalidated` (with reason + evidence) for verified work later disproved. Completed never
returns to pending; invalidation is a distinct, recorded transition.

## Guardian review

After acceptance, rollback, replan, resume and override, the Guardian checks plan health:
no duplicate IDs, version matches history, dependencies resolve, unresolved criteria stay
covered, completed steps carry checkpoints, the current step is executable. Corruption
blocks the run with an unblock hint; coverage gaps warn (the final verifier enforces
them at completion).
