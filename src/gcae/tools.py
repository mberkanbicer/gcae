from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import ToolCall, ToolResult


class WorkspaceViolation(ValueError):
    """Raised when a tool path escapes the active worktree."""


class DangerousCommand(ValueError):
    """Raised when a command matches a blocked destructive pattern."""


class ToolRegistry:
    def __init__(self, worktree: str | Path, command_timeout: int = 30) -> None:
        self.worktree = Path(worktree).resolve()
        self.command_timeout = command_timeout
        self._tools: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "apply_patch": self.apply_patch,
            "create_file": self.create_file,
            "run_command": self.run_command,
        }

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, call: ToolCall) -> ToolResult:
        if call.name not in self._tools:
            return ToolResult(tool=call.name, success=False, error="tool is not registered")
        try:
            return self._tools[call.name](call.arguments)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            return ToolResult(tool=call.name, success=False, error=str(exc))

    def _path(self, raw: str) -> Path:
        return self._inside((self.worktree / raw).resolve(), raw)

    def _inside(self, path: Path, raw: str) -> Path:
        if path != self.worktree and self.worktree not in path.parents:
            raise WorkspaceViolation(f"path escapes agent worktree: {raw}")
        return path

    def list_files(self, args: dict[str, Any]) -> ToolResult:
        root = self._path(str(args.get("path", ".")))
        pattern = str(args.get("pattern", "**/*"))
        files = sorted(
            str(self._inside(path.resolve(), str(path)).relative_to(self.worktree))
            for path in root.glob(pattern)
            if path.is_file()
        )
        return ToolResult(
            tool="list_files",
            success=True,
            output="\n".join(files),
            changed_files=[],
        )

    def read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        return ToolResult(tool="read_file", success=True, output=path.read_text(encoding="utf-8"))

    def search_text(self, args: dict[str, Any]) -> ToolResult:
        needle = str(args["query"])
        root = self._path(str(args.get("path", ".")))
        matches: list[str] = []
        for path in root.rglob("*"):
            if path.is_file() and ".git" not in path.parts:
                safe_path = self._inside(path.resolve(), str(path))
                try:
                    for number, line in enumerate(
                        safe_path.read_text(encoding="utf-8").splitlines(), 1
                    ):
                        if needle in line:
                            matches.append(
                                f"{safe_path.relative_to(self.worktree)}:{number}:{line}"
                            )
                except UnicodeDecodeError:
                    continue
        return ToolResult(tool="search_text", success=True, output="\n".join(matches))

    def create_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        if path.exists():
            raise ValueError("create_file refuses to overwrite an existing file")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return ToolResult(
            tool="create_file",
            success=True,
            changed_files=[str(path.relative_to(self.worktree))],
        )

    def apply_patch(self, args: dict[str, Any]) -> ToolResult:
        patch = str(args["patch"])
        self._check_patch_paths(patch)
        result = subprocess.run(
            ["git", "apply", "--whitespace=error"],
            cwd=self.worktree,
            input=patch,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise ValueError(result.stderr.strip() or "patch rejected")
        return ToolResult(tool="apply_patch", success=True, output=result.stdout, changed_files=[])

    def run_command(self, args: dict[str, Any]) -> ToolResult:
        command = str(args["command"])
        self._check_command(command)
        timeout = int(args.get("timeout", self.command_timeout))
        if timeout <= 0:
            raise ValueError("command timeout must be positive")
        result = subprocess.run(
            command,
            cwd=self.worktree,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return ToolResult(
            tool="run_command",
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr or None,
            exit_code=result.returncode,
        )

    @staticmethod
    def _check_command(command: str) -> None:
        lowered = command.lower()
        blocked = [
            r"\bsudo\b", r"\bshutdown\b", r"\breboot\b", r"\bmkfs(?:\.|\s)",
            r"\bdd\s+if=", r"rm\s+-rf\s+/(?:\s|$)", r"\bpoweroff\b",
            r"\b(?:curl|wget|nc|ssh|scp)\b",
            r"\b(?:pip|uv|poetry|npm)\s+(?:install|add|sync)\b",
            r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+pip\s+install\b",
            r"\bgit\s+(?:reset|clean|commit|worktree|checkout|switch|merge|rebase)\b",
        ]
        if any(re.search(pattern, lowered) for pattern in blocked):
            raise DangerousCommand("command is blocked as destructive")

    @staticmethod
    def _check_patch_paths(patch: str) -> None:
        for line in patch.splitlines():
            if not line.startswith(("--- ", "+++ ")):
                continue
            raw_path = line[4:].split("\t", 1)[0]
            if raw_path == "/dev/null":
                continue
            relative = raw_path[2:] if raw_path[:2] in {"a/", "b/"} else raw_path
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise WorkspaceViolation(f"patch path escapes agent worktree: {raw_path}")
