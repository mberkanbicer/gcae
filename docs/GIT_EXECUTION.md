# Git execution

The source repository must be a committed, clean Git repository. GCAE refuses dirty sources. It
creates exactly one external worktree and branch per run. Runtime-owned rollback performs
`reset --hard <accepted_commit>` and `clean -fdx` only inside that worktree. The source branch is
never merged or modified.

Only evaluator acceptance creates `gcae: <goal>` checkpoint commits. A passing final verification
with a non-empty worktree creates the final `gcae: verified final state` checkpoint, so
`accepted_commit` always equals the verified tree of a completed run. Resume verifies that the
persisted worktree is registered with the source repository before resetting it.

GCAE never merges on its own. After a successful run the CLI asks the user whether to merge the
run branch; `--merge` answers yes without prompting and `--no-merge` disables the question. The
same merge is available later as `gcae merge <repository> <run-id>`, which requires the run to be
complete and the run branch to still point at the verified commit. The merge requires a clean
source repository, prefers `--ff-only`, falls back to `--no-ff`, and aborts cleanly on conflicts.
The pre-merge and merge commits are recorded in `state.json`, and `gcae undo <repository> <run-id>`
resets the source branch back to the recorded pre-merge commit, refusing when the repository is
dirty or HEAD moved after the merge.

Before validation and final verification, runtime-owned cleanup removes generated Python/test
caches and ignored files from the isolated worktree only. This prevents test artifacts from being
checkpointed while preserving the source repository.
