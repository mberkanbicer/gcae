# Architecture

GCAE is a single-agent runtime with explicit Python orchestration. `Runtime` owns the lifecycle, `GitRepository` owns execution isolation, `MemoryStore` owns cumulative knowledge, and `StateStore` owns resumable JSON state. Pydantic models define all cross-subsystem contracts. Providers return validated decisions; the runtime, not the model, executes tools.

Each run creates one `gcae/<run-id>` branch and one worktree under the configured runtime directory. Checkpoint commits are trusted execution state; SQLite memory and JSONL events are cumulative knowledge and survive rollback.

Run artifacts are kept outside target repositories under `runs/<run-id>/`: `state.json`,
`events.jsonl`, `memory.db`, per-step tool results, and diff snapshots. The runtime removes
generated caches and ignored files only inside the isolated worktree before validation and final
verification.
