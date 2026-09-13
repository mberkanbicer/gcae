from __future__ import annotations

from pathlib import Path

from .models import AgentState, CriterionResult, ToolCall, VerificationReport
from .safeguards import check_hygiene
from .tools import ToolRegistry


class FinalVerifier:
    def verify(self, state: AgentState) -> VerificationReport:
        worktree = Path(state.worktree)
        if not worktree.exists():
            return VerificationReport(
                passed=False,
                criteria=[],
                hygiene_passed=False,
                details=["agent worktree does not exist"],
            )

        tools = ToolRegistry(worktree)
        results: list[CriterionResult] = []
        for criterion in state.success_criteria:
            results.append(self._verify_criterion(criterion, tools))
        passed = all(result.passed for result in results)
        missing = [result.criterion for result in results if not result.passed]
        hygiene_passed = self._hygiene(worktree)
        return VerificationReport(
            passed=passed,
            criteria=results,
            missing_requirements=missing,
            hygiene_passed=hygiene_passed,
            details=[] if hygiene_passed else ["workspace hygiene failed"],
        )

    def _verify_criterion(
        self,
        criterion: str,
        tools: ToolRegistry,
    ) -> CriterionResult:
        command = criterion.strip()
        if command.startswith("file exists: "):
            relative = command.removeprefix("file exists: ").strip()
            try:
                path = tools._path(relative)
                passed = path.is_file()
                evidence = str(path)
            except ValueError as exc:
                passed = False
                evidence = str(exc)
            return CriterionResult(criterion=criterion, passed=passed, evidence=evidence)

        if command.startswith("file contains: "):
            value = command.removeprefix("file contains: ")
            relative, separator, expected = value.partition(" :: ")
            if not separator:
                return CriterionResult(
                    criterion=criterion,
                    passed=False,
                    evidence="expected format: file contains: path :: text",
                )
            try:
                path = tools._path(relative.strip())
                passed = path.is_file() and expected in path.read_text(encoding="utf-8")
                evidence = str(path)
            except (OSError, UnicodeError, ValueError) as exc:
                passed = False
                evidence = str(exc)
            return CriterionResult(criterion=criterion, passed=passed, evidence=evidence)

        if command.startswith("command succeeds: "):
            shell_command = command.removeprefix("command succeeds: ").strip()
            result = tools.execute(
                ToolCall(name="run_command", arguments={"command": shell_command})
            )
            return CriterionResult(
                criterion=criterion,
                passed=result.success,
                evidence=result.output or result.error or "",
            )

        return CriterionResult(
            criterion=criterion,
            passed=False,
            evidence=(
                "unsupported criterion; use 'file exists: ', 'file contains: ', "
                "or 'command succeeds: '"
            ),
        )

    @staticmethod
    def _hygiene(worktree: Path) -> bool:
        return check_hygiene(worktree).passed
