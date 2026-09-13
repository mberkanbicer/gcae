# Git execution

The source repository must be a committed, clean Git repository. GCAE refuses dirty sources. It
creates exactly one external worktree and branch per run. Runtime-owned rollback performs
`reset --hard <accepted_commit>` and `clean -fdx` only inside that worktree. The source branch is
never merged or modified.

Only evaluator acceptance creates `gcae: <goal>` checkpoint commits. A passing final verification
with a non-empty worktree creates the final `gcae: verified final state` checkpoint, so
`accepted_commit` always equals the verified tree of a completed run. Resume verifies that the
persisted worktree is registered with the source repository before resetting it.

Before validation and final verification, runtime-owned cleanup removes generated Python/test
caches and ignored files from the isolated worktree only. This prevents test artifacts from being
checkpointed while preserving the source repository.
