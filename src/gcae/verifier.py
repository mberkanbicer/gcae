from __future__ import annotations

import json
from pathlib import Path

from .models import (
    AgentState,
    CriterionJudgement,
    CriterionResult,
    EvidenceRecord,
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


#: criteria prefixes _verify_criterion can check deterministically, without the judge
DETERMINISTIC_PREFIXES = (
    "file exists: ",
    "file contains: ",
    "file contains exactly: ",
    "command succeeds: ",
)


class FinalVerifier:
    """Deterministic verification with an optional, strict model judge.

    The judge is only consulted for criteria that have no deterministic check, must answer with
    structured evidence, and fails closed on any provider error or empty evidence.
    """

    def __init__(
        self, judge: Provider | None = None, sandbox_prefix: list[str] | None = None
    ) -> None:
        self.judge = judge
        #: optional command wrapper applied to `command succeeds:` criteria too, so
        #: verification runs under the same operator boundary as the step's own commands
        self.sandbox_prefix = list(sandbox_prefix or [])

    def verify(
        self,
        state: AgentState,
        diff: str = "",
        evidence: list[EvidenceRecord] | None = None,
    ) -> VerificationReport:
        """Verify every mandatory criterion against evidence, never against vibes.

        Completion requires PASS for every criterion.  A criterion with no evidence in the
        ledger is INSUFFICIENT, a criterion with contradicting evidence fails even when a
        check nominally passed, and a criterion verdict always carries the ledger ids it
        rests on.
        """
        worktree = Path(state.worktree)
        if not worktree.exists():
            return VerificationReport(
                passed=False,
                criteria=[],
                hygiene_passed=False,
                details=["agent worktree does not exist"],
            )
        ledger = list(evidence or [])
        tools = ToolRegistry(
            worktree, sandbox_prefix=self.sandbox_prefix, source_repo=state.source_repo
        )
        # Hygiene describes the candidate as the agent left it, so it is measured before
        # criterion commands run: a criterion that runs pytest creates __pycache__ and
        # would otherwise fail its own run's hygiene check.
        hygiene_passed = self._hygiene(worktree)
        results: list[CriterionResult] = []
        for criterion in state.success_criteria:
            results.append(self._verify_criterion(criterion, tools, state, diff, ledger))
        passed = all(result.status == "pass" for result in results)
        missing = [result.criterion for result in results if result.status != "pass"]
        return VerificationReport(
            passed=passed,
            criteria=results,
            missing_requirements=missing,
            hygiene_passed=hygiene_passed,
            details=[] if hygiene_passed else ["workspace hygiene failed"],
        )

    @staticmethod
    def _matching(
        evidence: list[EvidenceRecord], criterion: str
    ) -> tuple[list[EvidenceRecord], list[EvidenceRecord]]:
        """Ledger records that speak for or against a criterion (substring, both ways)."""
        target = criterion.lower()
        supporting: list[EvidenceRecord] = []
        contradicting: list[EvidenceRecord] = []
        for record in evidence:
            claims = " | ".join([record.claim_or_subject, *record.supports]).lower()
            claim_match = (
                record.claim_or_subject and record.claim_or_subject.lower() in target
            )
            contradictions = " | ".join(record.contradicts).lower()
            if target in contradictions or (claim_match and record.contradicts):
                contradicting.append(record)
            elif target in claims or claim_match:
                # an explicit contradiction beats an implicit claim match: a record that
                # says the criterion is false never counts as supporting it
                supporting.append(record)
        # the judge weighs recency, not a score: newest first, so a repaired run's fresh
        # supporting evidence outranks the stale contradiction it replaced (ISO strings
        # sort chronologically and never raise on naive/aware mixes)
        supporting.sort(key=lambda record: record.created_at.isoformat(), reverse=True)
        contradicting.sort(key=lambda record: record.created_at.isoformat(), reverse=True)
        return supporting, contradicting

    def _verify_criterion(
        self,
        criterion: str,
        tools: ToolRegistry,
        state: AgentState,
        diff: str,
        evidence: list[EvidenceRecord],
    ) -> CriterionResult:
        command = criterion.strip()
        if command.startswith("file exists: "):
            relative = command.removeprefix("file exists: ").strip()
            try:
                path = tools._path(relative)
                passed = path.is_file()
                text = str(path)
            except ValueError as exc:
                passed = False
                text = str(exc)
            return CriterionResult(
                criterion=criterion,
                passed=passed,
                status="pass" if passed else "fail",
                evidence=text,
            )

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
                text = evidence_for(path, expected, found)
            except (OSError, UnicodeError, ValueError) as exc:
                passed = False
                text = str(exc)
            return CriterionResult(
                criterion=criterion,
                passed=passed,
                status="pass" if passed else "fail",
                evidence=text,
            )

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
                text = evidence_for(path, expected, found)
            except (OSError, UnicodeError, ValueError) as exc:
                passed = False
                text = str(exc)
            return CriterionResult(
                criterion=criterion,
                passed=passed,
                status="pass" if passed else "fail",
                evidence=text,
            )

        if command.startswith("command succeeds: "):
            shell_command = command.removeprefix("command succeeds: ").strip()
            result = tools.execute(
                ToolCall(name="run_command", arguments={"command": shell_command})
            )
            return CriterionResult(
                criterion=criterion,
                passed=result.success,
                status="pass" if result.success else "fail",
                evidence=result.output or result.error or "",
            )

        if self.judge is not None:
            return self._judge_criterion(criterion, state, diff, evidence)

        return CriterionResult(
            criterion=criterion,
            passed=False,
            status="fail",
            evidence=(
                "unsupported criterion; use 'file exists: ', 'file contains: ', "
                "'file contains exactly: ', 'command succeeds: ' or enable "
                '[verifier] kind = "hybrid"'
            ),
        )

    def _judge_criterion(
        self,
        criterion: str,
        state: AgentState,
        diff: str,
        evidence: list[EvidenceRecord],
    ) -> CriterionResult:
        """A criterion the runtime cannot check deterministically: the ledger decides first.

        No evidence at all -> INSUFFICIENT (the task is not complete).  Contradicting
        evidence without support -> FAIL.  Otherwise the judge rules on the evidence bundle,
        and fails closed on any provider error or empty citation.
        """
        assert self.judge is not None
        supporting, contradicting = self._matching(evidence, criterion)
        if not supporting and not contradicting:
            return CriterionResult(
                criterion=criterion,
                passed=False,
                status="insufficient",
                evidence=(
                    "no evidence in the ledger speaks to this criterion; the task is not "
                    "complete until something observable supports it"
                ),
            )
        if contradicting and not supporting:
            cited = ", ".join(
                f"E{record.id} ({record.summary[:80]})" for record in contradicting[:3]
            )
            return CriterionResult(
                criterion=criterion,
                passed=False,
                status="fail",
                evidence=f"contradicted by evidence: {cited}",
                evidence_ids=[record.id for record in contradicting if record.id is not None],
            )
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
        cited_records = [
            {
                "id": record.id,
                "kind": str(record.kind),
                "claim": record.claim_or_subject[:160],
                "source": record.source_reference[:160],
                "summary": record.summary[:300],
                "contradicts": record.contradicts[:3],
                "created_at": record.created_at.isoformat(),
            }
            for record in [*supporting[:6], *contradicting[:3]]
        ]
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
            "ledger_evidence": cited_records,
        }
        schema = json.dumps(CriterionJudgement.model_json_schema(), separators=(",", ":"))
        prompt = (
            "You are the final verifier of GCAE, a reversible coding runtime. Decide whether the "
            "criterion is satisfied by the observable evidence. Judge only from the evidence "
            "provided; if the evidence is insufficient, answer passed=false. Never assume "
            "unobserved behavior. Reply with exactly one JSON object and no other text.\n"
            "When passed=true, evidence must cite the concrete observation that establishes it.\n"
            "ledger_evidence is newest first; weigh recent observations over stale ones.\n"
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
        contra_ids = {record.id for record in contradicting}
        supporting_ids = [
            record.id for record in supporting
            if record.id is not None and record.id not in contra_ids
        ]
        weighed_ids = supporting_ids + [
            record.id for record in contradicting if record.id is not None
        ]
        # a pass rests on its support; a fail weighed both sides, so it cites both
        return CriterionResult(
            criterion=criterion,
            passed=passed,
            status="pass" if passed else "fail",
            evidence=verdict,
            evidence_ids=supporting_ids if passed else weighed_ids,
        )

    @staticmethod
    def _hygiene(worktree: Path) -> bool:
        return check_hygiene(worktree).passed
