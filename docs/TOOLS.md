# Tools

The registry exposes only `list_files`, `read_file`, `search_text`, `apply_patch`, `create_file`,
and `run_command`. Arguments are:

| Tool          | Arguments                                             |
| ------------- | ----------------------------------------------------- |
| `list_files`  | `{path?: str, pattern?: str}`                         |
| `read_file`   | `{path: str}`                                         |
| `search_text` | `{query: str, path?: str}`                            |
| `apply_patch` | `{patch: str}` (unified diff)                         |
| `create_file` | `{path: str, content: str}`, refuses to overwrite     |
| `run_command` | `{command: str, timeout?: int}`                       |

File writes, reads and patching are confined to the active worktree, including patch-header checks.
Unregistered tool names return a failed `ToolResult` and never execute. Git checkpoint operations
remain runtime-owned and are blocked as commands.

Commands run in the worktree with a configurable timeout, and capture stdout, stderr and exit code.
The blocklist rejects `sudo`, `shutdown`, `reboot`, `poweroff`, `mkfs`, `dd if=`, recursive removal
of `/`, network download tools, package installation (`pip`, `pip3`, `uv`, `poetry`, `npm`), and
Git history/worktree commands (`reset`, `clean`, `commit`, `worktree`, `checkout`, `switch`,
`merge`, `rebase`).
