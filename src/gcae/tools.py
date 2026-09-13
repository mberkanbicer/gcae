from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import ToolCall, ToolResult

TOOL_DESCRIPTIONS: dict[str, str] = {
    "list_files": "list worktree-relative files; arguments {'path'?: str, 'pattern'?: str}",
    "read_file": "read a UTF-8 text file; arguments {'path': str}",
    "search_text": "search literal text in files; arguments {'query': str, 'path'?: str}",
    "apply_patch": "apply a unified diff inside the worktree; arguments {'patch': str}",
    "write_file": "overwrite or create a UTF-8 text file; arguments {'path': str, 'content': str}",
    "create_file": (
        "create one new file and refuse overwrite; arguments {'path': str, 'content': str}"
    ),
    "run_command": (
        "run a shell command in the worktree; arguments {'command': str, 'timeout'?: int}"
    ),
    "run_tests": "run the configured project test commands; arguments {}",
}


class WorkspaceViolation(ValueError):
    """Raised when a tool path escapes the active worktree."""


class DangerousCommand(ValueError):
    """Raised when a command matches a blocked destructive pattern."""


class ToolRegistry:
    def __init__(
        self,
        worktree: str | Path,
        command_timeout: int = 30,
        artifact_dir: str | Path | None = None,
        test_commands: list[str] | None = None,
        max_output_chars: int = 8000,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.command_timeout = command_timeout
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir is not None else None
        self.test_commands = list(test_commands or [])
        self.max_output_chars = max_output_chars
        self._artifact_counter = 0
        self._tools: dict[str, Callable[[dict[str, Any]], ToolResult]] = {
            "list_files": self.list_files,
            "read_file": self.read_file,
            "search_text": self.search_text,
            "apply_patch": self.apply_patch,
            "write_file": self.write_file,
            "create_file": self.create_file,
            "run_command": self.run_command,
            "run_tests": self.run_tests,
        }

    def names(self) -> list[str]:
        return sorted(self._tools)

    def execute(self, call: ToolCall) -> ToolResult:
        if call.name not in self._tools:
            return ToolResult(tool=call.name, success=False, error="tool is not registered")
        started = time.perf_counter()
        try:
            result = self._tools[call.name](call.arguments)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            result = ToolResult(tool=call.name, success=False, error=str(exc))
        result.duration_ms = round((time.perf_counter() - started) * 1000, 1)
        return self._externalize(result)

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
        return ToolResult(tool="list_files", success=True, output="\n".join(files))

    def read_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        return ToolResult(tool="read_file", success=True, output=path.read_text(encoding="utf-8"))

    def search_text(self, args: dict[str, Any]) -> ToolResult:
        needle = str(args["query"])
        root = self._path(str(args.get("path", ".")))
        matches: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            safe_path = self._inside(path.resolve(), str(path))
            try:
                if safe_path.stat().st_size > 1_000_000:
                    continue
                text = safe_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if needle in line:
                    matches.append(f"{safe_path.relative_to(self.worktree)}:{number}:{line}")
        return ToolResult(tool="search_text", success=True, output="\n".join(matches))

    def write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        if path.is_dir():
            raise ValueError("write_file target is a directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return ToolResult(
            tool="write_file",
            success=True,
            changed_files=[str(path.relative_to(self.worktree))],
        )

    def create_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        if path.exists():
            raise ValueError(
                "create_file refuses to overwrite an existing file; "
                "use write_file to replace its contents"
            )
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
        return ToolResult(tool="apply_patch", success=True, output=result.stdout)

    def run_command(self, args: dict[str, Any]) -> ToolResult:
        return self._shell(str(args["command"]), args.get("timeout"))

    def run_tests(self, args: dict[str, Any]) -> ToolResult:
        del args
        if not self.test_commands:
            raise ValueError("no test commands are configured for this project")
        outputs: list[str] = []
        success = True
        for command in self.test_commands:
            result = self._shell(command, None)
            success = success and result.success
            outputs.append(f"$ {command}\n{result.output}{result.error or ''}".rstrip())
        return ToolResult(tool="run_tests", success=success, output="\n\n".join(outputs))

    def _shell(self, command: str, timeout: Any) -> ToolResult:
        self._check_command(command)
        limit = int(timeout) if timeout is not None else self.command_timeout
        if limit <= 0:
            raise ValueError("command timeout must be positive")
        try:
            result = subprocess.run(
                command,
                cwd=self.worktree,
                shell=True,
                capture_output=True,
                text=True,
                timeout=limit,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                tool="run_command",
                success=False,
                error=f"command timed out after {limit}s: {command}",
            )
        return ToolResult(
            tool="run_command",
            success=result.returncode == 0,
            output=result.stdout,
            error=result.stderr or None,
            exit_code=result.returncode,
        )

    def _externalize(self, result: ToolResult) -> ToolResult:
        payload = result.output + (f"\n{result.error}" if result.error else "")
        if len(payload) <= self.max_output_chars or self.artifact_dir is None:
            if len(payload) > self.max_output_chars:
                result.output = payload[: self.max_output_chars] + "\n[output truncated]"
                result.error = None
            return result
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self._artifact_counter += 1
        artifact = self.artifact_dir / f"{result.tool}-{self._artifact_counter:04d}.txt"
        artifact.write_text(payload, encoding="utf-8")
        head = payload[: self.max_output_chars]
        result.output = f"{head}\n[output truncated; full output: {artifact}]"
        result.error = None
        result.artifact = str(artifact)
        return result

    @staticmethod
    def _check_command(command: str) -> None:
        lowered = command.lower()
        blocked = [
            r"\bsudo\b", r"\bshutdown\b", r"\breboot\b", r"\bmkfs(?:\.|\s)",
            r"\bdd\s+if=", r"rm\s+-rf\s+/(?:\s|$|\*)", r"\bpoweroff\b",
            r"\b(?:curl|wget|nc|ssh|scp)\b",
            r"\b(?:pip3?|uv|poetry|npm)\s+(?:install|add|sync)\b",
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
