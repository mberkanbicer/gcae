from __future__ import annotations

import ipaddress
import os
import re
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

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
    "read_files": (
        "read several UTF-8 text files at once; arguments {'paths': [str]}. "
        "Partial results: readable files are returned under '=== path ===' headers "
        "and failures are listed, with success=false when any file failed"
    ),
    "search_text": (
        "search file contents; arguments {'query': str, 'path'?: str, 'regex'?: bool, "
        "'include'?: str glob, 'context_lines'?: int, 'max_matches'?: int}. "
        "Literal by default; 'include' is a worktree-relative glob like '**/*.py'"
    ),
    "apply_patch": "apply a unified diff inside the worktree; arguments {'patch': str}",
    "write_file": "overwrite or create a UTF-8 text file; arguments {'path': str, 'content': str}",
    "write_files": (
        "write several UTF-8 text files at once; arguments {'files': [{path, content}]}. "
        "All-or-nothing: every path is validated first, and a mid-batch failure rolls back "
        "the files already written"
    ),
    "create_file": (
        "create one new file and refuse overwrite; arguments {'path': str, 'content': str}"
    ),
    "make_dirs": (
        "create directories inside the worktree; arguments {'paths': [str]}. "
        "Existing directories are fine; an existing file at any path fails the call"
    ),
    "edit_file": (
        "replace exact text in one file; arguments {'path': str, 'old_text': str, "
        "'new_text'?: str, 'replace_all'?: bool}. Fails when old_text is absent, or matches "
        "more than once without replace_all=true"
    ),
    "edit_files": (
        "apply exact-text edits to several files at once; "
        "arguments {'edits': [{path, old_text, new_text?, replace_all?}]}. "
        "All-or-nothing: every edit is validated before anything is written, and a "
        "mid-batch write failure rolls back the files already written"
    ),
    "fetch_url": (
        "fetch one http(s) URL and return its text; arguments {'url': str, "
        "'timeout'?: float, 'max_bytes'?: int}. Only public addresses are fetched; "
        "responses beyond max_bytes are truncated with a note"
    ),
    "run_command": (
        "run a shell command in the worktree; arguments {'command': str, 'timeout'?: int, "
        "'mode'?: 'batch'|'scripted_input'|'interactive_pty', 'stdin'?: [str], "
        "'interactive'?: bool, 'purpose'?: str, 'cwd'?: str worktree-relative dir, "
        "'env'?: {str: str} extra variables}. Use mode='scripted_input' with 'stdin' for a "
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
            "read_files": self.read_files,
            "search_text": self.search_text,
            "apply_patch": self.apply_patch,
            "write_file": self.write_file,
            "write_files": self.write_files,
            "create_file": self.create_file,
            "make_dirs": self.make_dirs,
            "edit_file": self.edit_file,
            "edit_files": self.edit_files,
            "fetch_url": self.fetch_url,
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

    def read_files(self, args: dict[str, Any]) -> ToolResult:
        entries = args.get("paths")
        if not isinstance(entries, list) or not entries:
            raise ValueError("read_files needs a non-empty 'paths' list")
        chunks: list[str] = []
        failed: list[str] = []
        for raw in entries:
            if not isinstance(raw, str):
                raise ValueError("read_files 'paths' must be strings")
            path = self._path(raw)
            header = str(path.relative_to(self.worktree))
            try:
                if path.stat().st_size > 1_000_000:
                    chunks.append(f"=== {header}: SKIPPED (over 1 MB) ===")
                    failed.append(raw)
                    continue
                chunks.append(f"=== {header} ===\n" + path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError) as exc:
                chunks.append(f"=== {header}: ERROR ({exc}) ===")
                failed.append(raw)
        if failed:
            return ToolResult(
                tool="read_files",
                success=False,
                output="\n".join(chunks),
                error=f"{len(failed)} of {len(entries)} files unreadable: " + ", ".join(failed),
            )
        return ToolResult(tool="read_files", success=True, output="\n".join(chunks))

    def search_text(self, args: dict[str, Any]) -> ToolResult:
        query = str(args["query"])
        root = self._path(str(args.get("path", ".")))
        include = str(args.get("include") or "**/*")
        context_lines = self._non_negative_int(args.get("context_lines", 0), "context_lines")
        max_matches = self._non_negative_int(args.get("max_matches", 200), "max_matches")
        matcher: Callable[[str], object]
        if args.get("regex"):
            try:
                pattern = re.compile(query)
            except re.error as exc:
                raise ValueError(f"invalid regex {query!r}: {exc}") from exc
            matcher = pattern.search
        else:
            matcher = lambda line: query in line  # noqa: E731 - tiny local predicate
        lines_out: list[str] = []
        shown = 0
        capped = False
        for path in sorted(root.glob(include)):
            if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
                continue
            safe_path = self._inside(path.resolve(), str(path))
            try:
                if safe_path.stat().st_size > 1_000_000:
                    continue
                file_lines = safe_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            relative = safe_path.relative_to(self.worktree)
            for number, line in enumerate(file_lines, 1):
                if not matcher(line):
                    continue
                if shown >= max_matches:
                    capped = True
                    break
                shown += 1
                start = max(1, number - context_lines)
                for extra in range(start, number):
                    lines_out.append(f"{relative}:{extra}-{file_lines[extra - 1]}")
                lines_out.append(f"{relative}:{number}:{line}")
                for extra in range(number + 1, min(len(file_lines), number + context_lines) + 1):
                    lines_out.append(f"{relative}:{extra}-{file_lines[extra - 1]}")
            if capped:
                break
        if capped:
            lines_out.append(f"[max_matches={max_matches} reached]")
        return ToolResult(tool="search_text", success=True, output="\n".join(lines_out))

    def write_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        if path.is_dir():
            raise ValueError("write_file target is a directory")
        self._write_atomically(path, str(args.get("content", "")), exclusive=False)
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
        try:
            self._write_atomically(path, str(args.get("content", "")), exclusive=True)
        except FileExistsError:
            raise ValueError(
                "create_file refuses to overwrite an existing file; "
                "use write_file to replace its contents"
            ) from None
        return ToolResult(
            tool="create_file",
            success=True,
            changed_files=[str(path.relative_to(self.worktree))],
        )

    @staticmethod
    def _write_atomically(path: Path, content: str, *, exclusive: bool) -> None:
        """Write via a sibling temp file so a crash never leaves a half-written file.

        ``exclusive`` links instead of replacing: the create fails atomically when the
        target appeared between the existence check and the write. A temp file may be
        left behind if the process dies mid-write; it carries a ``.gcae-tmp-`` prefix.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".gcae-tmp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
            if exclusive:
                os.link(tmp, path)
            else:
                os.replace(tmp, path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    @staticmethod
    def _non_negative_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    def write_files(self, args: dict[str, Any]) -> ToolResult:
        entries = args.get("files")
        if not isinstance(entries, list) or not entries:
            raise ValueError("write_files needs a non-empty 'files' list")
        targets: list[tuple[Path, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("write_files entries need {'path': str, 'content'?: str}")
            path = self._path(entry["path"])
            if path.is_dir():
                raise ValueError(f"write_files target is a directory: {entry['path']}")
            targets.append((path, str(entry.get("content", ""))))
        snapshots = self._snapshot([path for path, _ in targets])
        written: list[Path] = []
        try:
            for path, content in targets:
                self._write_atomically(path, content, exclusive=False)
                written.append(path)
        except OSError as exc:
            self._restore(snapshots)
            bad = str(written[-1].relative_to(self.worktree)) if written else "?"
            raise ValueError(
                f"write_files aborted at {bad} ({exc}); earlier writes rolled back"
            ) from exc
        return ToolResult(
            tool="write_files",
            success=True,
            output=f"wrote {len(targets)} files",
            changed_files=[str(path.relative_to(self.worktree)) for path, _ in targets],
        )

    def make_dirs(self, args: dict[str, Any]) -> ToolResult:
        entries = args.get("paths")
        if not isinstance(entries, list) or not entries:
            raise ValueError("make_dirs needs a non-empty 'paths' list")
        created: list[str] = []
        for raw in entries:
            if not isinstance(raw, str):
                raise ValueError("make_dirs 'paths' must be strings")
            path = self._path(raw)
            if path.is_file():
                raise ValueError(f"make_dirs path is an existing file: {raw}")
            path.mkdir(parents=True, exist_ok=True)
            created.append(str(path.relative_to(self.worktree)))
        return ToolResult(tool="make_dirs", success=True, output="\n".join(created))

    def edit_file(self, args: dict[str, Any]) -> ToolResult:
        path = self._path(str(args["path"]))
        new_text = self._checked_edit(path, args)
        self._write_atomically(path, new_text, exclusive=False)
        return ToolResult(
            tool="edit_file",
            success=True,
            changed_files=[str(path.relative_to(self.worktree))],
        )

    def edit_files(self, args: dict[str, Any]) -> ToolResult:
        entries = args.get("edits")
        if not isinstance(entries, list) or not entries:
            raise ValueError("edit_files needs a non-empty 'edits' list")
        planned: list[tuple[Path, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("edit_files entries need {'path', 'old_text', ...}")
            path = self._path(entry["path"])
            planned.append((path, self._checked_edit(path, entry)))
        snapshots = self._snapshot([path for path, _ in planned])
        written: list[Path] = []
        try:
            for path, new_text in planned:
                self._write_atomically(path, new_text, exclusive=False)
                written.append(path)
        except OSError as exc:
            self._restore(snapshots)
            raise ValueError(f"edit_files aborted ({exc}); earlier edits rolled back") from exc
        return ToolResult(
            tool="edit_files",
            success=True,
            output=f"edited {len(planned)} files",
            changed_files=[str(path.relative_to(self.worktree)) for path, _ in planned],
        )

    def _checked_edit(self, path: Path, args: dict[str, Any]) -> str:
        """New file text after validating the replacement; writes nothing."""
        if not isinstance(args.get("old_text"), str) or not args["old_text"]:
            raise ValueError("edit needs a non-empty 'old_text'")
        old_text: str = args["old_text"]
        new_text = str(args.get("new_text", ""))
        text = path.read_text(encoding="utf-8")
        found = text.count(old_text)
        if found == 0:
            raise ValueError(f"old_text not found in {path.relative_to(self.worktree)}")
        if found > 1 and not args.get("replace_all"):
            raise ValueError(
                f"old_text matches {found} times in {path.relative_to(self.worktree)}; "
                "set replace_all=true or narrow it to one occurrence"
            )
        return text.replace(old_text, new_text)

    def _snapshot(self, paths: list[Path]) -> dict[Path, bytes | None]:
        """Original bytes (None when absent) so a failed batch can be rolled back."""
        snapshots: dict[Path, bytes | None] = {}
        for path in paths:
            try:
                snapshots[path] = path.read_bytes() if path.exists() else None
            except OSError as exc:
                raise ValueError(
                    f"cannot snapshot {path.relative_to(self.worktree)}: {exc}"
                ) from exc
        return snapshots

    def _restore(self, snapshots: dict[Path, bytes | None]) -> None:
        """Best-effort rollback; never raises, so the original error stays visible."""
        for path, original in snapshots.items():
            try:
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(original)
            except OSError:
                continue

    def fetch_url(self, args: dict[str, Any]) -> ToolResult:
        url = self._check_public_url(str(args.get("url", "")))
        timeout = args.get("timeout", 10.0)
        limit = float(timeout) if timeout is not None else 10.0
        if limit <= 0:
            raise ValueError("fetch timeout must be positive")
        max_bytes = self._non_negative_int(args.get("max_bytes", 200_000), "max_bytes")
        if max_bytes == 0:
            raise ValueError("max_bytes must be positive")
        try:
            with httpx.Client(timeout=limit, follow_redirects=True, max_redirects=5) as client:
                response = client.get(url)
                response.raise_for_status()
                content = response.content
        except httpx.HTTPError as exc:
            raise ValueError(f"fetch failed: {exc}") from exc
        note = ""
        if len(content) > max_bytes:
            content = content[:max_bytes]
            note = f"\n[truncated to {max_bytes} bytes]"
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            text = content.decode("utf-8", errors="replace")
            note += " [decoded with replacements]"
        return ToolResult(tool="fetch_url", success=True, output=text + note)

    @staticmethod
    def _check_public_url(url: str) -> str:
        """Allow only http(s) URLs whose host resolves to public addresses.

        A guardrail, not a sandbox: DNS is resolved once up front, so a record that
        changes between the check and the fetch (rebinding) or a redirect into private
        space is a documented residual risk, handled the same way as the command
        blocklist — defense in depth, not a proof.
        """
        try:
            parts = urlsplit(url)
        except ValueError as exc:
            raise ValueError(f"not a valid URL: {url!r} ({exc})") from exc
        if parts.scheme not in ("http", "https"):
            raise ValueError(f"fetch_url needs an http(s) URL, got {url!r}")
        host = parts.hostname or ""
        if not host:
            raise ValueError(f"URL has no host: {url!r}")
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
        except socket.gaierror:
            raise ValueError(f"cannot resolve {host!r}") from None
        for info in infos:
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise ValueError(f"fetch_url refuses non-public address for {host!r}")
        return url

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
            cwd=args.get("cwd"),
            env=args.get("env"),
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
    def _extra_env(value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError("env must be an object mapping names to values")
        for key, item in value.items():
            if not isinstance(key, str) or not isinstance(item, str):
                raise ValueError("env names and values must both be strings")
        return dict(value)

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
        cwd: Any = None,
        env: Any = None,
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
        workdir = ""
        if cwd is not None:
            target = self._path(str(cwd))
            if not target.is_dir():
                raise ValueError(f"command cwd is not a directory: {cwd}")
            workdir = str(target)
        request = CommandRequest(
            command=command,
            mode=execution_mode,
            stdin=lines,
            timeout=limit,
            idle_timeout=self.runner.idle_timeout,
            startup_timeout=self.runner.startup_timeout,
            interactive=interactive,
            purpose=purpose,
            cwd=workdir,
            env=self._extra_env(env),
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
