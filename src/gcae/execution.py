"""Command execution: batch, scripted input, and interactive (PTY) processes.

One place decides *how* a command runs, so the runtime can stop treating every command as a
batch subprocess.  Three modes exist because real tasks need all three:

``BATCH``
    A command that completes on its own (``pytest -q``).  stdin is closed, so a program that
    reads from stdin sees end-of-file instead of hanging until a timeout.

``SCRIPTED_INPUT``
    A program that asks questions whose answers the agent may invent (a name, a menu choice,
    guesses in a game).  The lines are written to the process' stdin up front.

``INTERACTIVE_PTY``
    A program that needs a terminal: ``isatty()`` checks, ``getpass``, curses-style UIs, or
    anything that buffers differently when stdout is a pipe.  A pseudoterminal is allocated
    for exactly these cases and never for ordinary commands.

Timeouts are separated because one number cannot mean all of them: a process that produces no
output at all is not the same problem as one that runs too long, and a process waiting for the
user is not a failure at all.  When a program is waiting on stdin the execution returns
``waiting_for_input`` with the process still alive, so the runtime can ask the user and send
the answer back instead of burning an idle timeout.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import BinaryIO

try:  # pragma: no cover - platform guard; PTY mode is POSIX-only
    import pty as _pty
except ImportError:  # pragma: no cover - non-POSIX platforms
    _pty = None  # type: ignore[assignment]


class ExecutionMode(StrEnum):
    BATCH = "batch"
    SCRIPTED_INPUT = "scripted_input"
    INTERACTIVE_PTY = "interactive_pty"


class TimeoutKind(StrEnum):
    NONE = "none"
    STARTUP = "startup"
    IDLE = "idle"
    WALL = "wall"
    #: not a failure: the process is alive and asking for input we do not have
    USER_INPUT = "user_input"


class FailureKind(StrEnum):
    """Failure classes that change what the runtime does next.

    Deliberately small: a kind is only listed when some recovery policy depends on it.
    """

    NONE = "none"
    CODE_ERROR = "code_error"
    TEST_FAILURE = "test_failure"
    COMMAND_ERROR = "command_error"
    COMMAND_TIMEOUT = "command_timeout"
    INTERACTIVE_INPUT_REQUIRED = "interactive_input_required"
    FILE_NOT_FOUND = "file_not_found"
    PERMISSION_ERROR = "permission_error"
    DEPENDENCY_MISSING = "dependency_missing"
    INVALID_ARGUMENT = "invalid_argument"
    TOOL_ERROR = "tool_error"
    UNKNOWN = "unknown"


#: A line that looks like a program asking a human for something.
PROMPT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)(enter|type|provide|input|name|guess|choose|select|pick|continue|confirm|"
        r"press|password|passphrase|token|secret|key|answer|value|option)\b[^\n]{0,48}[:?>]\s*$"
    ),
    re.compile(r"(?i)^\s*(y/n|yes/no)\s*[:?]?\s*$"),
    re.compile(r"(?i)\[\s*(y/n|yes/no|q)\s*\]\s*$"),
    re.compile(r"(?i)\((y/n|yes/no)\)\s*$"),
)

#: Prompts whose answer must not be echoed into logs.
SENSITIVE_PATTERN = re.compile(
    r"(?i)\b(password|passphrase|secret|token|api[-_ ]?key|credential)\b"
)


@dataclass
class CommandRequest:
    """Everything the runner needs to know before it starts a process."""

    command: str
    mode: ExecutionMode = ExecutionMode.BATCH
    #: lines written to stdin (SCRIPTED_INPUT), or the first answer to a live prompt
    stdin: list[str] = field(default_factory=list)
    #: hard wall-clock limit in seconds
    timeout: float = 30.0
    #: no output for this long, while running, is a stalled process
    idle_timeout: float = 20.0
    #: nothing at all within this long means it never really started
    startup_timeout: float = 10.0
    #: the caller believes this program may ask for input
    interactive: bool = False
    #: what the command is for (shown in the UI, never executed)
    purpose: str = ""
    #: absolute working directory for this call; empty means the runner's worktree.
    #: Set only by the tool registry after workspace validation — never by the model.
    cwd: str = ""
    #: extra environment variables merged over the process environment for this call
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class CommandOutcome:
    """What actually happened, with the evidence needed to decide what to do next."""

    command: str
    cwd: str
    mode: ExecutionMode
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    started_at: float = 0.0
    duration_ms: float = 0.0
    timed_out: bool = False
    timeout_kind: TimeoutKind = TimeoutKind.NONE
    interactive_detected: bool = False
    waiting_for_input: bool = False
    prompt: str = ""
    sensitive_prompt: bool = False
    termination_reason: str = ""
    stdin_sent: int = 0
    failure_kind: FailureKind = FailureKind.NONE
    #: present only when the process is still alive awaiting input
    handle: RunningCommand | None = None

    @property
    def success(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and not self.waiting_for_input

    @property
    def combined(self) -> str:
        text = self.stdout
        if self.stderr:
            text = f"{text}\n{self.stderr}" if text else self.stderr
        return text

    def excerpt(self, limit: int = 4000) -> str:
        """The evidence to put in front of the model: both ends, never the middle only."""
        text = self.combined.strip()
        if len(text) <= limit:
            return text
        head = text[: limit // 2]
        tail = text[-(limit // 2) :]
        return f"{head}\n[... {len(text) - limit} characters omitted ...]\n{tail}"


class RunningCommand:
    """A process that is still alive: either a live interactive session or a stall."""

    def __init__(self, request: CommandRequest, cwd: Path, max_output: int) -> None:
        self.request = request
        self.cwd = Path(request.cwd) if request.cwd else cwd
        self.max_output = max_output
        self.started_at = time.monotonic()
        self.stdout = ""
        self.stderr = ""
        self.stdin_sent = 0
        self.interactive_detected = False
        self.termination_reason = ""
        #: the prompt already handed to the caller, so re-entering wait() after answering does
        #: not report the same question again
        self._reported_prompt = ""
        self._reported_at_length = 0
        self._stdout_lock = threading.Lock()
        self._stderr_lock = threading.Lock()
        self._output_event = threading.Event()
        self._epoll: object | None = None
        self._master: int | None = None
        self._process = self._spawn()

    # ------------------------------------------------------------------ spawning

    def _child_env(self) -> dict[str, str]:
        """Environment for the child: line-buffered output, nothing else changed.

        Without this, a Python program that prints a prompt and then waits on stdin keeps the
        prompt in an 8 KiB buffer, so the runtime sees silence instead of the question it was
        asked — and reports a timeout where the truth is "interactive".
        """
        env = dict(os.environ)
        env.update(self.request.env)
        # unbuffered output stays forced even if the caller set it: prompt detection
        # depends on seeing output as it happens, and a silent override would reintroduce
        # the buffered-prompt-misread-as-timeout failure this method documents above
        env["PYTHONUNBUFFERED"] = "1"
        env.setdefault("PYTHONIOENCODING", "utf-8")
        return env

    def _spawn(self) -> subprocess.Popen[bytes]:
        # start_new_session: one process group per command, so a timeout kills children too
        if self.request.mode is ExecutionMode.INTERACTIVE_PTY:
            if _pty is None:  # pragma: no cover - non-POSIX
                raise RuntimeError("interactive PTY mode is unavailable on this platform")
            master, slave = _pty.openpty()
            self._master = master
            process = subprocess.Popen(  # noqa: S603 - the caller validated the command
                self.request.command,
                shell=True,
                cwd=str(self.cwd),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                env=self._child_env(),
            )
            os.close(slave)
            self._start_reader(self._read_master, master)
            return process
        process = subprocess.Popen(  # noqa: S603 - the caller validated the command
            self.request.command,
            shell=True,
            cwd=str(self.cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=self._child_env(),
        )
        assert process.stdout is not None and process.stderr is not None
        self._start_reader(self._read_pipe, process.stdout, True)
        self._start_reader(self._read_pipe, process.stderr, False)
        return process

    def _start_reader(self, target: Callable[..., object], *args: object) -> None:
        threading.Thread(
            target=target, args=args, daemon=True, name="gcae-exec-reader"
        ).start()

    # ------------------------------------------------------------------ reading

    def _read_pipe(self, stream: BinaryIO, is_stdout: bool) -> None:
        """Read what is available, not "read until the buffer is full".

        ``stream.read(n)`` on a buffered pipe blocks until n bytes or EOF, which would hide
        every prompt until the process exited.  ``os.read`` returns as soon as anything is
        there, which is what an observant runtime needs.
        """
        lock = self._stdout_lock if is_stdout else self._stderr_lock
        try:
            descriptor = stream.fileno()
        except (OSError, ValueError):  # pragma: no cover - stream already detached
            return
        try:
            while True:
                chunk = os.read(descriptor, 4096)
                if not chunk:
                    break
                self._append(chunk, is_stdout, lock)
        except (OSError, ValueError):
            return

    def _read_master(self, master: int) -> None:
        try:
            while True:
                chunk = os.read(master, 4096)
                if not chunk:
                    break
                self._append(chunk, True, self._stdout_lock)
        except OSError:
            return

    def _append(self, raw: bytes, is_stdout: bool, lock: threading.Lock) -> None:
        text = raw.decode("utf-8", errors="replace")
        with lock:
            if is_stdout:
                self.stdout = (self.stdout + text)[-self.max_output :]
            else:
                self.stderr = (self.stderr + text)[-self.max_output :]
        self._output_event.set()

    # ------------------------------------------------------------------ interface

    def send(self, text: str) -> None:
        """Write one answer to the process (a line, newline added)."""
        stream = self._process.stdin
        if self._master is not None:
            os.write(self._master, (text + "\n").encode())
        elif stream is not None:
            stream.write((text + "\n").encode())
            stream.flush()
        else:
            raise RuntimeError("process stdin is closed")
        self.stdin_sent += 1

    def queue(self, lines: list[str]) -> None:
        for line in lines:
            self.send(line)

    def close_stdin(self) -> None:
        if self._master is None and self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except OSError:  # pragma: no cover - already closed
                pass

    def poll(self) -> int | None:
        return self._process.poll()

    def terminate(self, *, reason: str, grace: float = 0.5) -> None:
        """Ask politely, then insist; both signals go to the whole process group."""
        if self._process.poll() is not None:
            self.termination_reason = self.termination_reason or reason
            return
        self.termination_reason = reason
        self._signal_group(signal.SIGTERM)
        try:
            self._process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            self._signal_group(signal.SIGKILL)
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
                self.termination_reason = f"{reason} (process did not exit)"
        finally:
            self._close_master()

    def _signal_group(self, sig: int) -> None:
        try:
            os.killpg(os.getpgid(self._process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self._process.send_signal(sig)
            except (ProcessLookupError, OSError):  # pragma: no cover - already gone
                pass

    def _new_prompt(self) -> str:
        """The prompt to hand over, or empty when it was already handed over.

        A process that consumed our answer and asks a *new* question has grown its output,
        which is what distinguishes "still waiting for the same answer" from "asking again".
        """
        prompt = prompt_line(self.stdout)
        if not prompt:
            return ""
        if prompt == self._reported_prompt and len(self.stdout) <= self._reported_at_length:
            return ""
        if self.request.stdin and len(self.request.stdin) > self.stdin_sent:
            return ""  # queued answers are still going in
        return prompt

    def _close_master(self) -> None:
        if self._master is not None:
            try:
                os.close(self._master)
            except OSError:  # pragma: no cover - already closed
                pass
            self._master = None

    # ------------------------------------------------------------------ waiting

    def wait(
        self,
        *,
        timeout: float,
        idle_timeout: float,
        startup_timeout: float,
        allow_prompt_wait: bool = True,
    ) -> CommandOutcome:
        """Watch the process until it ends, stalls, or asks for input."""
        deadline = self.started_at + timeout
        last_output = self.started_at
        kind = TimeoutKind.NONE

        while True:
            code = self.poll()
            if code is not None:
                self._close_master()
                return self._outcome(code)
            now = time.monotonic()
            if self._output_event.is_set():
                self._output_event.clear()
                last_output = now
            idle = now - last_output
            if self.request.mode is ExecutionMode.SCRIPTED_INPUT:
                # the caller supplied answers; running out of them is a *result*, not a
                # reason to stop the run and ask a human
                exhausted = prompt_line(self.stdout)
                if exhausted and len(self.request.stdin) <= self.stdin_sent:
                    self.interactive_detected = True
                    self.terminate(reason="scripted input exhausted")
                    return self._outcome(self.poll(), prompt_lines=exhausted)
            if allow_prompt_wait:
                waiting_prompt = self._new_prompt()
                if waiting_prompt:
                    # alive and asking for something we do not have: not a failure, a question
                    self.interactive_detected = True
                    self._reported_prompt = waiting_prompt
                    self._reported_at_length = len(self.stdout)
                    return self._outcome(
                        None,
                        timeout_kind=TimeoutKind.USER_INPUT,
                        waiting_for_input=True,
                        prompt_lines=waiting_prompt,
                    )
            if idle >= idle_timeout:
                kind = TimeoutKind.IDLE if self.stdout or self.stderr else TimeoutKind.STARTUP
                break
            if now - self.started_at >= startup_timeout and not (self.stdout or self.stderr):
                kind = TimeoutKind.STARTUP
                break
            if now >= deadline:
                kind = TimeoutKind.WALL
                break
            time.sleep(0.02)

        # a batch command that printed a question and then stalled was interactive all along
        stalled_prompt = prompt_line(self.stdout)
        if stalled_prompt:
            self.interactive_detected = True
        self.terminate(reason=f"{kind.value} timeout")
        code = self.poll()
        return self._outcome(
            code, timeout_kind=kind, timed_out=True, prompt_lines=stalled_prompt or None
        )

    def _outcome(
        self,
        code: int | None,
        *,
        timeout_kind: TimeoutKind = TimeoutKind.NONE,
        timed_out: bool = False,
        waiting_for_input: bool = False,
        prompt_lines: str | None = None,
    ) -> CommandOutcome:
        duration_ms = round((time.monotonic() - self.started_at) * 1000, 1)
        prompt = prompt_lines or ""
        # A program that died because stdin was closed told us it wanted input: that is the
        # same discovery as catching it waiting, and the next strategy must know it.
        if not prompt and not waiting_for_input:
            candidate = prompt_line(self.stdout)
            if candidate and not timed_out:
                prompt = candidate
        detected = self.interactive_detected or bool(prompt) or waiting_for_input
        return CommandOutcome(
            command=self.request.command,
            cwd=str(self.cwd),
            mode=self.request.mode,
            exit_code=code,
            stdout=self.stdout,
            stderr=self.stderr,
            started_at=self.started_at,
            duration_ms=duration_ms,
            timed_out=timed_out,
            timeout_kind=timeout_kind,
            interactive_detected=detected,
            waiting_for_input=waiting_for_input,
            prompt=prompt,
            sensitive_prompt=bool(SENSITIVE_PATTERN.search(prompt)),
            termination_reason=self.termination_reason,
            stdin_sent=self.stdin_sent,
            failure_kind=classify_failure(
                exit_code=code,
                stdout=self.stdout,
                stderr=self.stderr,
                timed_out=timed_out,
                timeout_kind=timeout_kind,
                waiting_for_input=waiting_for_input,
                interactive_detected=self.interactive_detected,
                command=self.request.command,
            ),
            handle=self if waiting_for_input else None,
        )


def looks_interactive(prompt_text: str) -> bool:
    """True when a line of process output looks like a question for a human."""
    return any(pattern.search(prompt_text.strip()) for pattern in PROMPT_PATTERNS)


def prompt_line(text: str) -> str:
    """The last line of output when it reads like a prompt, otherwise an empty string."""
    lines = [line.strip() for line in text[-400:].splitlines() if line.strip()]
    if not lines:
        return ""
    last = lines[-1]
    for pattern in PROMPT_PATTERNS:
        if pattern.search(last):
            return last
    return ""


def classify_failure(
    *,
    exit_code: int | None,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
    timeout_kind: TimeoutKind = TimeoutKind.NONE,
    waiting_for_input: bool = False,
    interactive_detected: bool = False,
    command: str = "",
) -> FailureKind:
    """Turn raw execution evidence into the class of problem the runtime must solve."""
    del timeout_kind, command  # evidence already reflected in the flags below
    if waiting_for_input:
        return FailureKind.INTERACTIVE_INPUT_REQUIRED
    if exit_code == 0 and not timed_out:
        return FailureKind.NONE
    if timed_out:
        # a timed-out interactive program is an interactivity problem, not a slow command
        if interactive_detected:
            return FailureKind.INTERACTIVE_INPUT_REQUIRED
        return FailureKind.COMMAND_TIMEOUT
    if interactive_detected:
        return FailureKind.INTERACTIVE_INPUT_REQUIRED
    text = f"{stdout}\n{stderr}".lower()
    if re.search(r"eof when reading a line|eoferror|no tty|input required", text):
        return FailureKind.INTERACTIVE_INPUT_REQUIRED
    if re.search(r"no such file or directory|filenotfounderror|cannot find the path", text):
        return FailureKind.FILE_NOT_FOUND
    if re.search(r"permission denied|operation not permitted", text):
        return FailureKind.PERMISSION_ERROR
    if re.search(r"modulenotfounderror|importerror|command not found|no module named", text):
        return FailureKind.DEPENDENCY_MISSING
    if re.search(r"assertionerror|failed|failures?|error collecting", text):
        return FailureKind.TEST_FAILURE
    if re.search(
        r"syntaxerror|indentationerror|nameerror|typeerror|valueerror|keyerror|"
        r"indexerror|traceback",
        text,
    ):
        return FailureKind.CODE_ERROR
    if "usage:" in text or "unrecognized arguments" in text:
        return FailureKind.INVALID_ARGUMENT
    if exit_code is not None:
        return FailureKind.COMMAND_ERROR
    return FailureKind.UNKNOWN


def strategy_signature(command: str, error: str) -> str:
    """Stable fingerprint of "the same approach failing the same way".

    The command is normalised (whitespace, absolute paths, numbers) and the error is reduced
    to its first meaningful line, so a retry that changed nothing meaningful collides while a
    retry that fixed something does not.
    """
    normalized = re.sub(r"\s+", " ", command).strip()
    normalized = re.sub(r"(/[\w./-]+)+", lambda m: m.group(0).rsplit("/", 1)[-1], normalized)
    normalized = re.sub(r"\b\d+\b", "N", normalized)
    detail = ""
    for line in (error or "").splitlines():
        stripped = line.strip()
        if stripped:
            detail = re.sub(r"\b\d+\b", "N", stripped)[:160]
            break
    return f"{normalized} :: {detail}"


class CommandRunner:
    """Runs one command in a chosen mode and returns evidence, never a bare "timeout"."""

    def __init__(
        self,
        worktree: str | Path,
        *,
        default_timeout: float = 30.0,
        idle_timeout: float = 20.0,
        startup_timeout: float = 10.0,
        grace: float = 0.5,
        max_output: int = 200_000,
        max_scripted_lines: int = 200,
    ) -> None:
        self.worktree = Path(worktree).resolve()
        self.default_timeout = default_timeout
        self.idle_timeout = idle_timeout
        self.startup_timeout = startup_timeout
        self.grace = grace
        self.max_output = max_output
        self.max_scripted_lines = max_scripted_lines

    def run(self, request: CommandRequest) -> CommandOutcome:
        """Start the command and wait for a result (which may be "asking for input")."""
        if not request.stdin and request.mode is ExecutionMode.SCRIPTED_INPUT:
            raise ValueError("scripted_input mode needs at least one stdin line")
        running = self.start(request)
        return self.wait(running)

    def start(self, request: CommandRequest) -> RunningCommand:
        request.timeout = request.timeout if request.timeout > 0 else self.default_timeout
        if request.stdin:
            # a queued answer is what makes a prompt routable without a human
            request.interactive = True
        running = RunningCommand(request, self.worktree, self.max_output)
        if request.stdin:
            running.queue(request.stdin[: self.max_scripted_lines])
        if request.mode is not ExecutionMode.INTERACTIVE_PTY and not request.interactive:
            # nothing will ever be written: hand the child end-of-file instead of a hang
            running.close_stdin()
        return running

    def wait(self, running: RunningCommand) -> CommandOutcome:
        request = running.request
        return running.wait(
            timeout=request.timeout,
            idle_timeout=request.idle_timeout,
            startup_timeout=request.startup_timeout,
            # Only a caller who asked for it waits for a human.  A scripted run reports the
            # unanswered prompt as evidence instead of pausing the whole run.
            allow_prompt_wait=request.interactive
            and request.mode is not ExecutionMode.SCRIPTED_INPUT,
        )
