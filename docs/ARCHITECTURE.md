# Architecture

GCAE is a single-agent runtime with explicit Python orchestration. `Runtime` owns the lifecycle,
`GitRepository` owns execution isolation, `MemoryStore` owns cumulative knowledge, and `StateStore`
owns resumable JSON state. Pydantic models define all cross-subsystem contracts. Providers return
validated decisions; the runtime, not the model, executes tools.

`Planner` produces an `InitialPlan` (objective, criteria, constraints, assumptions, steps). In V1
the planner is deterministic: the objective, criteria and constraints come from the caller, the
plan is one semantic step, and assumptions are empty. The controller, evaluator and final verifier
are separate components.

The evaluator receives an `EvaluationInput` (objective, semantic goal, constraints, observations,
accepted commit, reconstructed context, and deterministic validation) and returns a validated
`Evaluation`. `DeterministicEvaluator` is the default; `LLMEvaluator` asks the configured model for
the same contract and can promote failure memories through `memories_to_promote`. Select it with
`[evaluator] kind = "llm"` in the configuration.

Each run creates one `gcae/<run-id>` branch and one worktree under the configured runtime
directory. Checkpoint commits are trusted execution state; SQLite memory and JSONL events are
cumulative knowledge and survive rollback. Only evaluator acceptance, or a passing final
verification with a non-empty worktree, creates a checkpoint; completion therefore always points
`accepted_commit` at the verified tree.

The context given to the model is rebuilt from persistent state on every call. It is not
conversation history and no history is accumulated. `ContextBuilder` budgets the reconstructed
sections by estimated tokens.

Run artifacts are kept outside target repositories: `memory.db` at the state root is cumulative
and shared across runs, while `runs/<run-id>/` retains `state.json`, `events.jsonl`, per-step tool
results, and diff snapshots. The runtime removes generated caches and ignored files only inside the
isolated worktree before validation and final verification.

`python -m gcae` prints the final `AgentState` as JSON on stdout and a short human-readable summary
on stderr. After a verified run the CLI asks whether to merge the run branch; a confirmed merge is
recorded with its pre-merge commit and can be reversed with `gcae undo`.
