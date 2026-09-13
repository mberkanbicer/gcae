from __future__ import annotations

from pathlib import Path

from .git import GitRepository
from .models import ToolResult, ValidationResult
from .tools import ToolRegistry


class DeterministicValidator:
    def __init__(
        self,
        repo: GitRepository,
        tools: ToolRegistry,
        commands: list[str] | None = None,
    ) -> None:
        self.repo = repo
        self.tools = tools
        self.commands = commands or []

    def validate(self, intended_scope: list[str] | None = None) -> ValidationResult:
        command_results: list[ToolResult] = []
        for command in self.commands:
            command_results.append(self.tools.run_command({"command": command}))
        entries = self.repo.status_entries()
        changed = [path for _, path in entries]
        scope_violations = []
        if intended_scope:
            scope_violations = [
                file
                for file in changed
                if not any(
                    file == scope or file.startswith(f"{scope}/")
                    for scope in intended_scope
                )
            ]
        deleted = [path for code, path in entries if "D" in code]
        new_files = [path for code, path in entries if code == "??"]
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
        dependency_changes = [
            file for file in changed if Path(file).name in dependency_names
        ]
        diff_check_passed = self.repo.diff_check()
        details = [
            f"command {index + 1}: {'passed' if result.success else 'failed'}"
            for index, result in enumerate(command_results)
        ]
        if dependency_changes:
            details.append(f"dependency manifests changed: {', '.join(dependency_changes)}")
        passed = (
            diff_check_passed
            and all(result.success for result in command_results)
            and not scope_violations
        )
        return ValidationResult(
            passed=passed,
            command_results=command_results,
            diff_check_passed=diff_check_passed,
            changed_files=changed,
            new_files=new_files,
            deleted_files=deleted,
            dependency_changes=dependency_changes,
            scope_violations=scope_violations,
            details=details,
        )
