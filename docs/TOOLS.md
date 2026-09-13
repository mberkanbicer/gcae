# Tools

The registry exposes only `list_files`, `read_file`, `search_text`, `apply_patch`, `create_file`, and `run_command`. File writes and patching are confined to the active worktree, including symlink and patch-header checks. Git checkpoint operations remain runtime-owned. Commands use a timeout and reject sudo, shutdown, reboot, formatting, network download, package installation, and Git checkpoint operations.
