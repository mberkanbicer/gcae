from __future__ import annotations

import json
from pathlib import Path

from .models import (
    AgentState,
    CriterionJudgement,
    CriterionResult,
    ToolCall,
    VerificationReport,
)
from .providers import Provider, ProviderOutputError
from .safeguards import check_hygiene
from .tools import ToolRegistry


def evidence_for(path: Path, expected: str, found: str | None, limit: int = 200) -> str:
    """Show what was expected and what is actually there, so failures are diagnosable."""
    if found is None:
        return f"{path}: file does not exist"
    if found == expected:
        return str(path)
    actual = found if len(found) <= limit else found[:limit] + "…"
    wanted = expected if len(expected) <= limit else expected[:limit] + "…"
    return f"{path}: expected {wanted!r}, found {actual!r}"


class FinalVerifier:
    """Deterministic verification with an optional, strict model judge.

    The judge is only consulted for criteria that have no deterministic check, must answer with
    structured evidence, and fails closed on any provider error or empty evidence.
    """

    def __init__(self, judge: Provider | None = None) -> None:
        self.judge = judge

    def verify(self, state: AgentState, diff: str = "") -> VerificationReport:
        worktree = Path(state.worktree)
        if not worktree.exists():
            return VerificationReport(
                passed=False,
                criteria=[],
                hygiene_passed=False,
                details=["agent worktree does not exist"],
            )

        tools = ToolRegistry(worktree)
        # Hygiene describes the candidate as the agent left it, so it is measured before
        # criterion commands run: a criterion that runs pytest creates __pycache__ and
        # would otherwise fail its own run's hygiene check.
        hygiene_passed = self._hygiene(worktree)
        results: list[CriterionResult] = []
        for criterion in state.success_criteria:
            results.append(self._verify_criterion(criterion, tools, state, diff))
        passed = all(result.passed for result in results)
        missing = [result.criterion for result in results if not result.passed]
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
        state: AgentState,
        diff: str,
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

        if command.startswith("file contains exactly: "):
            value = command.removeprefix("file contains exactly: ")
            relative, separator, expected = value.partition(" :: ")
            if not separator:
                return CriterionResult(
                    criterion=criterion,
                    passed=False,
                    evidence="expected format: file contains exactly: path :: text",
                )
            try:
                path = tools._path(relative.strip())
                found = path.read_text(encoding="utf-8") if path.is_file() else None
                # "exactly" is about the content, not the final byte: a trailing newline at
                # end of file is conventional, and criteria are often inferred from prose.
                passed = found is not None and found.rstrip("\n") == expected.rstrip("\n")
                evidence = evidence_for(path, expected, found)
            except (OSError, UnicodeError, ValueError) as exc:
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
                found = path.read_text(encoding="utf-8") if path.is_file() else None
                passed = found is not None and expected in found
                evidence = evidence_for(path, expected, found)
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

        if self.judge is not None:
            return self._judge_criterion(criterion, state, diff)

        return CriterionResult(
            criterion=criterion,
            passed=False,
            evidence=(
                "unsupported criterion; use 'file exists: ', 'file contains: ', "
                "'file contains exactly: ', 'command succeeds: ' or enable "
                '[verifier] kind = "hybrid"'
            ),
        )

    def _judge_criterion(self, criterion: str, state: AgentState, diff: str) -> CriterionResult:
        assert self.judge is not None
        validation = state.latest_validation
        names: list[str] = []
        if validation is not None:
            names = list(validation.new_files)
            names.extend(
                name for name in validation.changed_files if name not in names
            )
        samples: dict[str, str] = {}
        worktree = Path(state.worktree)
        for name in names[:5]:
            path = worktree / name
            try:
                if path.is_file() and path.stat().st_size <= 20_000:
                    samples[name] = path.read_text(encoding="utf-8", errors="replace")[:2000]
            except OSError:
                continue
        evidence_bundle = {
            "objective": state.objective,
            "accepted_commit": state.accepted_commit,
            "worktree": state.worktree,
            "changed_files": validation.changed_files if validation is not None else [],
            "diff_stat": validation.diff_stat if validation is not None else "",
            "validation_passed": validation.passed if validation is not None else None,
            "command_results": [
                {
                    "tool": result.tool,
                    "success": result.success,
                    "output": result.output[:500],
                }
                for result in (validation.command_results if validation is not None else [])
            ],
            "worktree_samples": samples,
        }
        schema = json.dumps(CriterionJudgement.model_json_schema(), separators=(",", ":"))
        prompt = (
            "You are the final verifier of GCAE, a reversible coding runtime. Decide whether the "
            "criterion is satisfied by the observable evidence. Judge only from the evidence "
            "provided; if the evidence is insufficient, answer passed=false. Never assume "
            "unobserved behavior. Reply with exactly one JSON object and no other text.\n"
            "When passed=true, evidence must cite the concrete observation that establishes it.\n"
            f"Criterion: {criterion}\n"
            f"CriterionJudgement JSON schema: {schema}\n"
            f"Evidence: {json.dumps(evidence_bundle, separators=(',', ':'))}\n"
            f"Accepted diff (truncated):\n{diff[:8000]}"
        )
        try:
            judgement = self.judge.complete(prompt, CriterionJudgement)
        except ProviderOutputError as exc:
            return CriterionResult(
                criterion=criterion,
                passed=False,
                evidence=f"judge unavailable, criterion not established: {exc}",
            )
        passed = judgement.passed and bool(judgement.evidence.strip())
        verdict = judgement.evidence.strip() or "judge returned no evidence"
        return CriterionResult(criterion=criterion, passed=passed, evidence=verdict)

    @staticmethod
    def _hygiene(worktree: Path) -> bool:
        return check_hygiene(worktree).passed
