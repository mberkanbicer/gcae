# Workspace hygiene

Tools resolve paths and reject escapes. Commands run in the agent worktree and block obvious
destructive system operations. Deterministic validation checks command results, `git diff --check`,
changed files, dependency manifests, and scope. Runtime cleanup removes generated caches and
ignored artifacts only inside the agent worktree.

The final hygiene gate (`safeguards.check_hygiene`) rejects backup files (`~`), bytecode (`.pyc`),
and log files (`.log`) anywhere in the worktree before completion. Unsupported natural-language
completion criteria fail closed instead of producing a false success.
