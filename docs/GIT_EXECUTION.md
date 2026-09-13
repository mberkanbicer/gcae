# Git execution

The source repository must be a committed, clean Git repository. GCAE refuses dirty sources. It creates exactly one external worktree and branch per run. Runtime-owned rollback performs `reset --hard <accepted_commit>` and `clean -fd` only inside that worktree. The source branch is never merged or modified.

Before validation and final verification, runtime-owned cleanup removes generated Python/test
caches and ignored files from the isolated worktree only. This prevents test artifacts from being
checkpointed while preserving the source repository.
