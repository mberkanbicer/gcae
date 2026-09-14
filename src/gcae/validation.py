from __future__ import annotations

from pathlib import Path

from .git import GitRepository
from .models import ToolCall, ToolResult, ValidationResult
from .tools import ToolRegistry


def _path_like(entry: str) -> bool:
    """Planner prose (`file creation`, `script naming`) is not a path and cannot scope anything."""
    flat = entry.strip()
    return bool(flat) and " " not in flat and flat not in {".", ".."}


def _in_scope(path: str, intended_scope: list[str]) -> bool:
    for raw in intended_scope:
        if not _path_like(raw):
            continue
        scope = raw.strip().lstrip("./").rstrip("/")
        if not scope:
            return True
        if path == scope or path.startswith(f"{scope}/"):
            return True
        # a bare file name also matches the same file in a subdirectory
        if "/" not in scope and Path(path).name == scope:
            return True
    return not any(_path_like(entry) for entry in intended_scope)


class DeterministicValidator:
    """Cheap deterministic evidence collected before any semantic evaluation."""

    def __init__(
        self,
        repo: GitRepository,
        tools: ToolRegistry,
        commands: list[str] | None = None,
        scope_warning_files: int = 10,
    ) -> None:
        self.repo = repo
        self.tools = tools
        self.commands = commands or []
        self.scope_warning_files = scope_warning_files

    def validate(self, intended_scope: list[str] | None = None) -> ValidationResult:
        command_results: list[ToolResult] = []
        for command in self.commands:
            command_results.append(
                self.tools.execute(
                    ToolCall(name="run_command", arguments={"command": command})
                )
            )
        entries = self.repo.status_entries()
        changed = [path for _, path in entries]
        deleted = [path for code, path in entries if "D" in code]
        new_files = [path for code, path in entries if code == "??"]
        new_set = set(new_files)
        # Scope is evidence, never a gate: a plan's scope is a guess, and treating a guess as a
        # hard failure makes a run impossible whenever the plan is wrong (observed: a planner
        # writing prose such as "file creation" into intended_scope rolled back every attempt
        # to create the file the task asked for). Creating a new file is additive work and is
        # never a violation; modifications to existing files outside the guessed scope are
        # reported so the evaluator and the user can judge them.
        scope_violations = [
            file
            for file in changed
            if file not in new_set and not _in_scope(file, intended_scope or [])
        ]
        dependency_names = {
            "pyproject.toml",
            "requirements.txt",
            "requirements-dev.txt",
            "poetry.lock",
            "uv.lock",
            "Pipfile",
            "Pipfile.lock",
            "package.json",
            "package-lock.json",
            "yarn.lock",
        }
        dependency_changes = [file for file in changed if Path(file).name in dependency_names]
        diff_check_passed = self.repo.diff_check()
        diff_stat = self.repo.diff_stat()
        warnings: list[str] = []
        if len(changed) > self.scope_warning_files:
            warnings.append(
                f"scope warning: {len(changed)} changed files exceeds {self.scope_warning_files}"
            )
        if dependency_changes:
            warnings.append(f"dependency manifests changed: {', '.join(dependency_changes)}")
        if scope_violations:
            warnings.append(
                "scope warning: changed outside the step's intended scope: "
                + ", ".join(scope_violations)
            )
        if deleted and not intended_scope:
            warnings.append(f"files deleted: {', '.join(deleted)}")
        details = [
            f"command {index + 1}: {'passed' if result.success else 'failed'}"
            for index, result in enumerate(command_results)
        ]
        unresolved = self.repo.conflict_marker_files()
        if unresolved:
            warnings.append(f"unresolved merge conflicts: {', '.join(unresolved)}")
        passed = (
            diff_check_passed
            and all(result.success for result in command_results)
            and not unresolved
        )
        return ValidationResult(
            passed=passed,
            commands=list(self.commands),
            command_results=command_results,
            diff_check_passed=diff_check_passed,
            diff_stat=diff_stat,
            changed_files=changed,
            new_files=new_files,
            deleted_files=deleted,
            dependency_changes=dependency_changes,
            scope_violations=scope_violations,
            warnings=warnings,
            details=details,
        )
