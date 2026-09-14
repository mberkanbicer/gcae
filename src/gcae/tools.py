from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .execution import (
    CommandOutcome,
    CommandRequest,
    CommandRunner,
    ExecutionMode,
    RunningCommand,
)
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
        "run a shell command in the worktree; arguments {'command': str, 'timeout'?: int, "
        "'mode'?: 'batch'|'scripted_input'|'interactive_pty', 'stdin'?: [str], "
        "'interactive'?: bool, 'purpose'?: str}. Use mode='scripted_input' with 'stdin' for a "
        "program that asks questions you can answer yourself, mode='interactive_pty' when it "
        "needs a terminal, and 'interactive': true when it may ask for input you do not have"
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
        idle_timeout: float = 20.0,
        startup_timeout: float = 10.0,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.command_timeout = command_timeout
        self.artifact_dir = Path(artifact_dir).resolve() if artifact_dir is not None else None
        self.test_commands = list(test_commands or [])
        self.max_output_chars = max_output_chars
        self._artifact_counter = 0
        self.runner = CommandRunner(
            self.worktree,
            default_timeout=float(command_timeout),
            idle_timeout=idle_timeout,
            startup_timeout=startup_timeout,
        )
        #: a process left alive because it is waiting for the user
        self.pending_process: RunningCommand | None = None
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
        return self._shell(
            str(args["command"]),
            args.get("timeout"),
            mode=str(args.get("mode") or ExecutionMode.BATCH),
            stdin=args.get("stdin"),
            interactive=bool(args.get("interactive")),
            purpose=str(args.get("purpose") or ""),
        )

    def run_tests(self, args: dict[str, Any]) -> ToolResult:
        del args
        if not self.test_commands:
            raise ValueError("no test commands are configured for this project")
        outputs: list[str] = []
        success = True
        for command in self.test_commands:
            result = self._shell(command, None, mode=ExecutionMode.BATCH)
            success = success and result.success
            outputs.append(f"$ {command}\n{result.output}{result.error or ''}".rstrip())
        return ToolResult(tool="run_tests", success=success, output="\n\n".join(outputs))

    @staticmethod
    def _execution_mode(value: str) -> ExecutionMode:
        try:
            return ExecutionMode(value)
        except ValueError as exc:
            allowed = ", ".join(mode.value for mode in ExecutionMode)
            raise ValueError(f"unknown execution mode {value!r}; use one of: {allowed}") from exc

    @staticmethod
    def _stdin_lines(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [line for line in value.splitlines() if line != ""]
        if isinstance(value, list):
            return [str(line) for line in value]
        raise ValueError("stdin must be a string or a list of strings")

    def _shell(
        self,
        command: str,
        timeout: Any,
        *,
        mode: str | ExecutionMode = ExecutionMode.BATCH,
        stdin: Any = None,
        interactive: bool = False,
        purpose: str = "",
    ) -> ToolResult:
        self._check_command(command)
        limit = float(timeout) if timeout is not None else float(self.command_timeout)
        if limit <= 0:
            raise ValueError("command timeout must be positive")
        execution_mode = mode if isinstance(mode, ExecutionMode) else self._execution_mode(mode)
        lines = self._stdin_lines(stdin)
        if lines and execution_mode is ExecutionMode.BATCH:
            # answers given means the caller expects a conversation
            execution_mode = ExecutionMode.SCRIPTED_INPUT
        request = CommandRequest(
            command=command,
            mode=execution_mode,
            stdin=lines,
            timeout=limit,
            idle_timeout=self.runner.idle_timeout,
            startup_timeout=self.runner.startup_timeout,
            interactive=interactive,
            purpose=purpose,
        )
        outcome = self.runner.run(request)
        if outcome.waiting_for_input and outcome.handle is not None:
            self.pending_process = outcome.handle
        elif outcome.handle is None:
            self.pending_process = None
        return self._to_result(outcome, tool="run_command")

    def answer_pending(self, text: str) -> ToolResult | None:
        """Send the user's answer to the live process and report how it went."""
        handle = self.pending_process
        if handle is None:
            return None
        handle.send(text)
        outcome = self.runner.wait(handle)
        self.pending_process = outcome.handle
        result = self._to_result(outcome, tool="run_command")
        result.prompt = outcome.prompt or result.prompt
        return result

    def _to_result(self, outcome: CommandOutcome, *, tool: str) -> ToolResult:
        stdout = outcome.stdout.strip()
        stderr = outcome.stderr.strip()
        error: str | None = stderr or None
        if outcome.timed_out and not error:
            error = (
                f"command {outcome.timeout_kind} timeout after "
                f"{outcome.duration_ms / 1000:.1f}s: {outcome.command}"
            )
        if outcome.waiting_for_input:
            error = None
        return ToolResult(
            tool=tool,
            success=outcome.success,
            output=stdout,
            error=error,
            exit_code=outcome.exit_code,
            mode=str(outcome.mode),
            cwd=outcome.cwd,
            timed_out=outcome.timed_out,
            timeout_kind=str(outcome.timeout_kind),
            interactive_detected=outcome.interactive_detected,
            waiting_for_input=outcome.waiting_for_input,
            prompt=outcome.prompt,
            termination_reason=outcome.termination_reason,
            stdin_sent=outcome.stdin_sent,
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
