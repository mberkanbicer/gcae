# Tools

The registry exposes fourteen tools. Arguments are validated by the runtime; unknown tool names
return a failed `ToolResult` and never execute. Git checkpoint operations are runtime-owned and not
available to the model.

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `list_files` | `{path?, pattern?}` | list files under a worktree-relative path |
| `read_file` | `{path}` | read a UTF-8 text file |
| `read_files` | `{paths}` | read several files at once; partial results with per-file headers, `success=false` when any failed |
| `search_text` | `{query, path?, regex?, include?, context_lines?, max_matches?}` | literal text search by default; `regex: true` for patterns, `include` glob (e.g. `**/*.py`) to narrow files, `context_lines` for surrounding lines (`-` prefix), `max_matches` cap (default 200); skips `.git`, caches and files > 1 MB |
| `apply_patch` | `{patch}` | `git apply --whitespace=error` with header path checks |
| `write_file` | `{path, content}` | overwrite or create a text file |
| `write_files` | `{files: [{path, content}]}` | write several files; all-or-nothing with rollback |
| `create_file` | `{path, content}` | create a new file; refuses to overwrite |
| `make_dirs` | `{paths}` | create directories; existing dirs are fine, existing files fail |
| `edit_file` | `{path, old_text, new_text?, replace_all?}` | exact-text replacement; fails when absent or ambiguous |
| `edit_files` | `{edits: [{path, old_text, ...}]}` | batch exact-text edits; validated before anything is written, rollback on failure |
| `fetch_url` | `{url, timeout?, max_bytes?}` | fetch one public http(s) URL as text; truncated past `max_bytes` |
| `run_command` | `{command, timeout?, cwd?, env?}` | shell command in the worktree; `cwd` is a worktree-relative directory, `env` maps extra variable names to values |
| `run_tests` | `{}` | runs the configured `validation.commands` |

Every result carries `success`, `output`, `error`, `exit_code`, `duration_ms`, `changed_files` and,
when the output was externalized, `artifact`.

## Execution modes

`run_command` chooses how the process runs, because the same command needs different treatment
depending on the program:

| Mode | When | What it does |
| --- | --- | --- |
| `batch` (default) | ordinary commands (`pytest -q`) | stdin is closed, so a program that reads stdin gets end-of-file instead of hanging; startup, idle and wall timeouts are enforced |
| `scripted_input` | a program asking questions whose answers can be invented (`{"stdin": ["3", "9", "7"]}`) | answers are written up front; running out of answers is reported as evidence (`interactive input required`), never as a request to the user |
| `interactive_pty` | a program that checks `isatty()`, uses `getpass`, or needs a terminal; `{"interactive": true}` when it may ask for something only the user has | a real pseudoterminal for stdin/stdout/stderr; the process can stay alive while the user answers |

`run_command` also accepts `purpose` (a human sentence for the UI) and `timeout`. Commands run with
`PYTHONUNBUFFERED=1` in their own process group, and a timeout terminates the whole group
(SIGTERM, then SIGKILL), so no orphan is left behind.

## Reliability

File writes go through a sibling temp file and `os.replace`, so a crash never leaves a
half-written file. `create_file` links instead of replacing, so a file that appears between
the existence check and the write still fails atomically instead of being overwritten.

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
