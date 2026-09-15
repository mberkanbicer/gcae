# Trajectory model

The primary execution entity is a **trajectory step** — one semantic attempt — not a message,
tool call, or turn. `TrajectoryStep` (`src/gcae/models.py`) is persisted in `state.json`
(bounded to the last 20) and published as `trajectory_step_started` / `trajectory_step_completed`
events, so the dashboard and the recovery advisor read the same record.

```
TrajectoryStep
    id                        trajectory-<plan-step>-<attempt>
    semantic_goal             what this attempt is trying to achieve
    parent_plan_step_id       the plan step it serves
    started_at / completed_at

    expectation               what succeeding looks like (from the plan step)
    expected_evidence         observable checks that would prove it
    failure_signals           observations that would disprove it

    actions                   tool summaries (bounded)
    observations              what actually happened (bounded)
    validations               deterministic validation outcome

    candidate_base_commit     trusted state this attempt started from
    candidate_result_commit   the checkpoint it produced (accepted only)

    decision / decision_reason
    knowledge_gained          lessons, kept even when the attempt is rejected
    evidence_ids              ledger records collected during the attempt

    status                    PREPARING → EXECUTING → OBSERVING → VALIDATING → EVALUATING
                              → ACCEPTED | REJECTED | REPAIRED | REPLANNED | BLOCKED
```

One attempt can span many tool calls; the trajectory closes when the evaluator (or the
verifier, or recovery) decides its fate. A repair or a replan opens a fresh attempt
(`trajectory-<step>-<attempt+1>`), so the history of rejected strategies is visible without
reconstructing any chat transcript.

The planner supplies `expected_evidence` and `failure_signals` per plan step; the runtime
checks failure signals against observed output and records a *contradicting* evidence record
when one matches — expectation before action, observation after it, both first-class.
