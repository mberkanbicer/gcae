# Workspace hygiene

Every accepted change must be correct, necessary, clean and maintainable. The runtime actively
checks for repository dirt and fails closed instead of declaring success.

## Deterministic evidence

Per semantic step, `validation.py` collects: configured command results, `git diff --check`,
`git diff --stat`, changed/new/deleted files, dependency-manifest changes, intended-scope
violations and soft warnings (changed-file count over `scope_warning_files`, deleted files,
dependency manifests). Evidence is stored on the step and rendered into the evaluator context.

## Cleanup

Before validation and verification the runtime removes generated caches (`__pycache__`,
`.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.coverage`, `*.pyc`) and ignored files from the
isolated worktree only. Rollback additionally removes speculative untracked files with
`clean -fdx` inside that worktree.

## Final hygiene gate

Completion requires `safeguards.check_hygiene` to pass: no backup files (`~`), no bytecode
(`.pyc`), no debug logs (`.log`). Unsupported success criteria fail verification rather than
producing a false success.

## Behavioral expectations enforced elsewhere

- Dependency additions and deletions are surfaced to the evaluator and warned about; new
  dependencies, new files and broad refactors need justification in the accepted step.
- Commit history on the run branch is semantic (`gcae: <goal>`) instead of `fix`/`temp` noise.
- Runtime state never lands in the target repository; run artifacts live under the XDG state
  directory.
