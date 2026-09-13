# Tools

The registry exposes eight tools. Arguments are validated by the runtime; unknown tool names
return a failed `ToolResult` and never execute. Git checkpoint operations are runtime-owned and not
available to the model.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `list_files` | `{path?, pattern?}` | list files under a worktree-relative path |
| `read_file` | `{path}` | read a UTF-8 text file |
| `search_text` | `{query, path?}` | literal text search; skips `.git`, caches and files > 1 MB |
| `apply_patch` | `{patch}` | `git apply --whitespace=error` with header path checks |
| `write_file` | `{path, content}` | overwrite or create a text file |
| `create_file` | `{path, content}` | create a new file; refuses to overwrite |
| `run_command` | `{command, timeout?}` | shell command in the worktree |
| `run_tests` | `{}` | runs the configured `validation.commands` |

Every result carries `success`, `output`, `error`, `exit_code`, `duration_ms`, `changed_files` and,
when the output was externalized, `artifact`.

## Boundaries

All filesystem operations resolve inside the active worktree; path traversal, symlink escapes and
patch headers that point outside are rejected. Tool writes never reach the user's repository, the
runtime state directory is written only by runtime-owned code, and the only writable locations are
the worktree and `runs/<run-id>/artifacts`.

## Command safety

Commands run with the worktree as cwd, a configurable timeout and captured stdout/stderr/exit code.
The blocklist rejects `sudo`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd if=`, filesystem-wide
recursive deletion, network download tools, package installation (`pip`/`pip3`/`uv`/`poetry`/`npm`)
and Git history/worktree commands (`reset`, `clean`, `commit`, `worktree`, `checkout`, `switch`,
`merge`, `rebase`). This is a guardrail, not an OS sandbox.

## Output handling

When combined output exceeds `max_output_chars` (default 8000), the full text is written to
`runs/<run-id>/artifacts/<tool>-NNNN.txt` and the context receives a truncated head plus the
artifact path. Large outputs therefore never flood the context budget.
