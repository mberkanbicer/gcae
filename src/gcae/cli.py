from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from .config import Config, ProviderConfig, discover_config, load_config
from .evaluator import DeterministicEvaluator, Evaluator, LLMEvaluator
from .git import GitError, GitRepository, MergeConflict, NothingToMerge
from .http_provider import OpenAICompatibleProvider
from .models import AgentState, MergeRecord, now_utc
from .persistence import StateStore
from .planner import LLMPlanner, Planner
from .providers import FakeProvider, Provider
from .runtime import (
    RunLock,
    Runtime,
    RuntimeControl,
    cleanup_idle_worktree,
    cleanup_merged_worktree,
    merge_verified_run,
)
from .verifier import FinalVerifier

ROLES = ("controller", "planner", "evaluator", "verifier", "escalation", "recovery")

#: provider kinds served by the OpenAI-compatible HTTP provider
HTTP_KINDS = frozenset({"http", "openrouter", "google"})
#: Google's OpenAI-compatible endpoint (chat completions, streaming, structured output).
#: The native `/v1beta/interactions` API is a different protocol and is not supported.
GOOGLE_OPENAI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"


def _version() -> str:
    """The installed distribution version, so packaging metadata stays the single source."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - stdlib since 3.8
        return "unknown"
    try:
        return version("gcae")
    except PackageNotFoundError:  # running from a source tree without installation
        return "0.0.0+source"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gcae", description="Git-Checkpointed Adaptive Execution")
    parser.add_argument("--version", action="version", version=_version())
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_runtime_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--config", type=Path, help="config file to use instead of auto-discovery"
        )
        sub.add_argument(
            "--runtime-dir",
            type=Path,
            help="directory for runs, worktrees and memory (defaults from config)",
        )
        mode = sub.add_mutually_exclusive_group()
        sub.add_argument(
            "--no-auto-bootstrap",
            action="store_true",
            help="refuse to start when the repository needs a base commit instead of creating one",
        )
        mode.add_argument("--tui", action="store_true", help="force the interactive TUI")
        mode.add_argument("--headless", action="store_true", help="force non-interactive output")

    run = subparsers.add_parser(
        "run",
        help="start a run: plan the task, execute it in an isolated worktree, verify it",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            '  gcae run ~/src/proj "Add a --dry-run flag to the importer"\n'
            '  gcae run ~/src/proj -p "Fix the parser" '
            '--criterion "command succeeds: pytest -q"\n'
            "  gcae run ~/src/proj --tui\n"
            "a request on the command line runs headlessly and saves ./result.json"
        ),
    )
    run.add_argument(
        "repository", type=Path, help="path to the repository root to work on"
    )
    run.add_argument(
        "request",
        nargs="?",
        default=None,
        help="task description; optional in TUI mode, where it is requested interactively",
    )
    run.add_argument(
        "--request",
        "-p",
        dest="request_flag",
        metavar="TEXT",
        default=None,
        help="task description (same as the positional form; a request on the command "
        "line runs headlessly — pass --tui to open the TUI with it pre-filled)",
    )
    add_runtime_flags(run)
    run.add_argument(
        "--constraint",
        action="append",
        default=[],
        help="a hard constraint the work must respect (repeatable)",
    )
    run.add_argument(
        "--criterion",
        action="append",
        default=[],
        help="a checkable success criterion, e.g. 'command succeeds: pytest -q' (repeatable)",
    )
    run.add_argument(
        "--merge", action="store_true", help="merge the verified branch without asking"
    )
    run.add_argument("--no-merge", action="store_true", help="never merge the run branch")

    resume = subparsers.add_parser(
        "resume", help="continue a stopped, failed or waiting run where it left off"
    )
    resume.add_argument("repository", type=Path, help="path to the repository root")
    resume.add_argument("run_id", help="run id from `gcae list`")
    resume.add_argument(
        "--force",
        action="store_true",
        help="override a blocked or waiting run instead of holding it for an answer",
    )
    add_runtime_flags(resume)

    input_parser = subparsers.add_parser(
        "input", help="send input to a process waiting for the user"
    )
    input_parser.add_argument("repository", type=Path, help="path to the repository root")
    input_parser.add_argument("run_id", help="run id from `gcae list`")
    input_parser.add_argument("text", help="the value the running process is waiting for")
    input_parser.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    input_parser.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )
    input_parser.add_argument("--headless", action="store_true", help=argparse.SUPPRESS)

    listing = subparsers.add_parser("list", help="list known runs")
    listing.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    listing.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )

    inspect = subparsers.add_parser("inspect", help="show a run summary")
    inspect.add_argument("run_id", help="run id from `gcae list`")
    inspect.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    inspect.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )
    inspect.add_argument("--json", action="store_true", help="dump the full persisted state")

    undo = subparsers.add_parser(
        "undo", help="reverse a run's merge, restoring the checkout to its prior state"
    )
    undo.add_argument("repository", type=Path, help="path to the repository root")
    undo.add_argument("run_id", help="run id from `gcae list`")
    undo.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    undo.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )

    prune = subparsers.add_parser("prune", help="delete the oldest run records")
    prune.add_argument(
        "--keep", type=int, default=10, help="keep this many newest runs (default: 10)"
    )
    prune.add_argument("--dry-run", action="store_true", help="list what would be deleted")
    prune.add_argument(
        "--force",
        action="store_true",
        help="also delete records whose merge is still recorded (their `gcae undo` is lost)",
    )
    prune.add_argument(
        "--older-than",
        type=float,
        default=None,
        help="also prune runs older than this many days (overrides [runtime] run_retention_days)",
    )
    prune.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    prune.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )

    merge = subparsers.add_parser(
        "merge", help="merge a completed run branch into the current branch"
    )
    merge.add_argument("repository", type=Path, help="path to the repository root")
    merge.add_argument("run_id", help="run id from `gcae list`")
    merge.add_argument(
        "--config", type=Path, help="config file to use instead of auto-discovery"
    )
    merge.add_argument(
        "--runtime-dir",
        type=Path,
        help="directory for runs, worktrees and memory (defaults from config)",
    )
    return parser


def _provider(provider_config: ProviderConfig) -> Provider:
    kind = provider_config.kind.lower()
    if kind in HTTP_KINDS:
        base_url = provider_config.base_url
        model = provider_config.model
        api_key_env = provider_config.api_key_env
        if kind == "google":
            fields = ProviderConfig.model_fields
            if base_url == fields["base_url"].default:
                base_url = GOOGLE_OPENAI_BASE_URL
            elif (
                (urlparse(base_url).hostname or "") == "generativelanguage.googleapis.com"
                and not urlparse(base_url).path.startswith("/v1beta/openai")
            ):
                raise ValueError(
                    f"kind 'google' needs the OpenAI-compatible endpoint "
                    f"({GOOGLE_OPENAI_BASE_URL}), got {base_url!r}; the native "
                    "`/v1beta/interactions` API is a different protocol"
                )
            if model == fields["model"].default:
                raise ValueError(
                    'provider.model is required for kind "google" '
                    "(e.g. model = \"gemini-2.5-flash\")"
                )
            if provider_config.api_key is None and api_key_env is None:
                api_key_env = "GEMINI_API_KEY"
        local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        api_key = provider_config.api_key or (
            os.environ.get(api_key_env) if api_key_env else None
        )
        host = urlparse(base_url).hostname or ""
        if not api_key and host not in local_hosts:
            raise ValueError(
                f"no API key configured for {base_url}; "
                "set provider.api_key or provider.api_key_env"
            )
        return OpenAICompatibleProvider(
            base_url=base_url,
            model=model,
            api_key=provider_config.api_key,
            api_key_env=api_key_env,
            timeout=provider_config.timeout,
            context_limit=provider_config.context_limit,
            generation=provider_config.generation.model_dump(),
            json_mode=provider_config.json_mode,
            stream=provider_config.stream,
            stall_timeout=provider_config.stall_timeout,
            retries=provider_config.retries,
            retry_backoff=provider_config.retry_backoff,
            min_request_interval=provider_config.min_request_interval,
        )
    if kind == "fake":
        return FakeProvider(
            [
                {
                    "action": "finish_candidate",
                    "semantic_goal": "finish",
                    "reason_summary": "fake provider",
                }
            ]
        )
    raise ValueError(f"unsupported provider kind: {provider_config.kind!r}")


def _providers(config: Config) -> tuple[Provider, dict[str, Provider]]:
    base = _provider(config.provider)
    roles: dict[str, Provider] = {}
    for role in ROLES:
        resolved = getattr(config.models, role).resolved(config.provider)
        if resolved is not None:
            roles[role] = _provider(resolved)
    return base, roles


def _evaluator(config: Config, provider: Provider) -> Evaluator:
    kind = config.evaluator.kind.lower()
    if kind == "deterministic":
        return DeterministicEvaluator()
    if kind == "llm":
        return LLMEvaluator(provider)
    raise ValueError(f"unsupported evaluator kind: {config.evaluator.kind!r}")


def _verifier(config: Config, provider: Provider) -> FinalVerifier:
    kind = config.verifier.kind.lower()
    if kind == "deterministic":
        return FinalVerifier()
    if kind == "hybrid":
        return FinalVerifier(provider)
    raise ValueError(f"unsupported verifier kind: {config.verifier.kind!r}")


def _planner(config: Config, provider: Provider) -> Planner | LLMPlanner:
    kind = config.planner.kind.lower()
    if kind == "auto":
        kind = (
            "llm"
            if config.provider.kind.lower() in HTTP_KINDS
            else "deterministic"
        )
    if kind == "deterministic":
        return Planner()
    if kind == "llm":
        return LLMPlanner(provider)
    raise ValueError(f"unsupported planner kind: {config.planner.kind!r}")


def _run_files(state: AgentState, repo: GitRepository) -> list[str]:
    """Files this run produced, computed against the right base even after a merge."""
    base = state.merge.pre_merge_commit if state.merge is not None else repo.current_branch()
    try:
        return repo.files_between(base, state.branch)
    except GitError:
        validation = state.latest_validation
        return list(validation.changed_files) if validation is not None else []


def _aligned(rows: list[tuple[str, str]]) -> str:
    """Render `label: value` rows with one shared colon column (plain ASCII, pipe-safe)."""
    width = max(len(label) for label, _ in rows)
    lines = []
    for label, value in rows:
        first, *rest = value.split("\n")
        lines.append(f"  {label:<{width}}: {first}")
        lines.extend(f"  {' ' * width}  {line}" for line in rest)
    return "\n".join(lines)


def _summary(state: AgentState, files: list[str] | None = None) -> str:
    files = files or []
    verification = state.last_verification
    if verification is None:
        criteria = "no verification"
    else:
        passed = sum(1 for item in verification.criteria if item.passed)
        criteria = f"{passed}/{len(verification.criteria)} criteria passed"
    if state.merge is None and not files:
        branch = f"{state.branch} (no file changes; nothing to merge)"
    elif state.merge is None:
        branch = (
            f"{state.branch} (not merged yet — GCAE merges automatically; "
            f"run 'gcae merge {state.source_repo} {state.run_id}' if it stayed pending)"
        )
    else:
        branch = (
            f"{state.branch} merged into {state.merge.target_branch} "
            f"(undo: gcae undo {state.source_repo} {state.run_id})"
        )
    rows = [
        ("accepted steps", f"{state.accepted_steps}, commit: {state.accepted_commit or 'none'}"),
        ("verification", criteria),
        ("worktree", f"{state.worktree}"),
        ("branch", branch),
    ]
    if files:
        listing = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
        rows.append(("files", listing))
    if not files:
        rows.append(("documents", "none — the run produced no files"))
    elif state.merge is not None:
        rows.append(("documents", f"{state.source_repo} (in your working tree now)"))
    else:
        rows.append(
            (
                "documents",
                f"{state.worktree} (worktree on branch {state.branch}; "
                "nothing is in your checkout until GCAE merges it)",
            )
        )
    if state.pending_question:
        rows.append(("question", state.pending_question))
        rows.append(
            (
                "answer with",
                f"gcae resume {state.source_repo} {state.run_id} (or press i in the dashboard)",
            )
        )
    return f"run {state.run_id}: {state.status}\n" + _aligned(rows)


def _state_path(runtime_dir: Path, run_id: str) -> Path:
    return Path(runtime_dir).expanduser() / "runs" / run_id / "state.json"


def _load_run(repository: Path, run_id: str, runtime_dir: Path) -> tuple[AgentState, Path]:
    state_path = _state_path(runtime_dir, run_id)
    state = StateStore(state_path).load()
    if Path(state.source_repo).resolve() != Path(repository).resolve():
        raise RuntimeError(f"repository does not match persisted run {run_id}")
    return state, state_path


def _apply_merge(
    state: AgentState,
    state_path: Path,
    repo: GitRepository,
    allow_unverified: bool = False,
    cleanup: bool = True,
) -> MergeRecord:
    with RunLock(repo.runtime_dir, Path(state.source_repo), "merge"):
        record = merge_verified_run(
            repo,
            state,
            persist=lambda: StateStore(state_path).save(state),
            allow_unverified=allow_unverified,
        )
    print(
        f"gcae: merged {record.branch} into {record.target_branch} "
        f"({record.pre_merge_commit[:12]} -> {record.merge_commit[:12]})",
        file=sys.stderr,
    )
    for notice in repo.notices:
        print(f"gcae: {notice.get('message')}", file=sys.stderr)
    repo.notices.clear()
    if cleanup and cleanup_merged_worktree(repo, state):
        print(
            f"gcae: removed the merged worktree {state.worktree} "
            f"(branch {record.branch} kept; gcae undo reverses the merge)",
            file=sys.stderr,
        )
    return record


def _undo(repository: Path, run_id: str, runtime_dir: Path) -> None:
    state, state_path = _load_run(repository, run_id, runtime_dir)
    merge = state.merge
    if merge is None:
        raise RuntimeError(f"run {run_id} has no recorded merge to undo")
    with RunLock(runtime_dir, Path(state.source_repo), f"undo {run_id}"):
        repo = GitRepository(state.source_repo, Path(runtime_dir).expanduser())
        repo.undo_merge(merge.pre_merge_commit, merge.merge_commit)
        state.merge = None
        StateStore(state_path).save(state)
    print(
        f"gcae: reversed merge of {merge.branch} into {merge.target_branch}; "
        f"HEAD is back at {merge.pre_merge_commit[:12]}",
        file=sys.stderr,
    )


def _merge_run(
    repository: Path, run_id: str, runtime_dir: Path, cleanup: bool = True
) -> None:
    state, state_path = _load_run(repository, run_id, runtime_dir)
    repo = GitRepository(state.source_repo, Path(runtime_dir).expanduser())
    if state.status != "complete" and state.accepted_steps > 0:
        print(
            f"gcae: warning: run {run_id} ended {state.status!r}; merging "
            f"{state.accepted_steps} accepted step(s) without final verification",
            file=sys.stderr,
        )
    try:
        _apply_merge(state, state_path, repo, allow_unverified=True, cleanup=cleanup)
    except NothingToMerge as exc:
        print(f"gcae: {exc}", file=sys.stderr)
        if cleanup and cleanup_idle_worktree(repo, state):
            print(f"gcae: removed the worktree of the empty run {state.worktree}", file=sys.stderr)
        return
    print(f"gcae: undo with: gcae undo {state.source_repo} {run_id}", file=sys.stderr)


def _maybe_merge(
    result: AgentState,
    runtime_dir: Path,
    merge_flag: bool,
    no_merge_flag: bool,
    auto_merge: bool = True,
    merge_accepted: bool = True,
    cleanup_after_merge: bool = True,
) -> None:
    if no_merge_flag or result.merge is not None or not result.branch:
        return
    complete = result.status == "complete"
    if not complete:
        # a failed or stopped run still owns the checkpoints it accepted
        if not (merge_accepted and result.accepted_steps > 0):
            return
        approved = True
    elif merge_flag or auto_merge:
        approved = True
    elif sys.stdin.isatty():
        print(
            f"merge {result.branch} into the current branch? [y/N] ",
            end="",
            file=sys.stderr,
            flush=True,
        )
        try:
            approved = input().strip().lower() in {"y", "yes"}
        except (EOFError, KeyboardInterrupt):
            approved = False
    else:
        print(
            f"gcae: branch {result.branch} is ready but automatic merging is off "
            "(auto_merge = false); press M in the dashboard or run "
            f"gcae merge {result.source_repo} {result.run_id}",
            file=sys.stderr,
        )
        return
    if not approved:
        return
    repo = GitRepository(result.source_repo, Path(runtime_dir).expanduser())
    try:
        _apply_merge(
            result,
            _state_path(runtime_dir, result.run_id),
            repo,
            allow_unverified=not complete,
            cleanup=cleanup_after_merge,
        )
    except NothingToMerge as exc:
        print(f"gcae: {exc}", file=sys.stderr)
        if cleanup_after_merge and cleanup_idle_worktree(repo, result):
            print(
                f"gcae: removed the worktree of the empty run {result.worktree}",
                file=sys.stderr,
            )
    except MergeConflict:
        raise
    except GitError as exc:
        print(f"gcae: merge skipped: {exc}", file=sys.stderr)


def _list_runs(runtime_dir: Path) -> None:
    runs_dir = Path(runtime_dir).expanduser() / "runs"
    rows: list[AgentState] = []
    if runs_dir.is_dir():
        for state_file in sorted(runs_dir.glob("*/state.json")):
            try:
                rows.append(StateStore(state_file).load())
            except (OSError, ValueError) as exc:
                print(f"gcae: skipping {state_file}: {exc}", file=sys.stderr)
    if not rows:
        print("no runs found")
        return
    rows.sort(key=lambda state: state.updated_at, reverse=True)
    table = [
        (
            state.run_id,
            f"{state.status}",
            str(state.accepted_steps),
            state.updated_at.isoformat(timespec="seconds"),
            state.objective.replace("\n", " ")[:60],
        )
        for state in rows
    ]
    width_id = max(len("run id"), max(len(row[0]) for row in table))
    width_status = max(len("status"), max(len(row[1]) for row in table))
    width_steps = max(len("steps"), max(len(row[2]) for row in table))
    width_updated = max(len("updated"), max(len(row[3]) for row in table))
    print(
        f"{'run id':<{width_id}}  {'status':<{width_status}}  "
        f"{'steps':>{width_steps}}  {'updated':<{width_updated}}  objective"
    )
    for run_id, status, steps, updated, objective in table:
        print(
            f"{run_id:<{width_id}}  {status:<{width_status}}  "
            f"{steps:>{width_steps}}  {updated:<{width_updated}}  {objective}"
        )


def _prune_runs(
    runtime_dir: Path,
    keep: int,
    dry_run: bool,
    force: bool = False,
    older_than_days: float | None = None,
) -> None:
    """Delete old run records: beyond `keep`, plus runs older than the retention TTL.

    Never touches a run whose repository lock is held — that is a live run in another
    process, however old its record is.

    A recorded merge is the only piece of run data that git does not already have (the
    pre-merge/merge commit pair), so `gcae undo` stops working without it; such records are
    kept unless `force` says otherwise."""
    import shutil

    runs_dir = Path(runtime_dir).expanduser() / "runs"
    if older_than_days is not None and older_than_days <= 0:
        raise ValueError("--older-than must be a positive number of days")
    if not runs_dir.is_dir():
        print("no runs found")
        return
    states: list[tuple[AgentState, Path]] = []
    for state_file in sorted(runs_dir.glob("*/state.json")):
        try:
            states.append((StateStore(state_file).load(), state_file.parent))
        except (OSError, ValueError) as exc:
            print(f"gcae: skipping {state_file}: {exc}", file=sys.stderr)
    states.sort(key=lambda pair: pair[0].updated_at, reverse=True)
    cutoff = now_utc() - timedelta(days=older_than_days) if older_than_days is not None else None
    pruned = 0
    for index, (state, run_dir) in enumerate(states):
        if index < keep and (cutoff is None or state.updated_at >= cutoff):
            continue
        if state.merge is not None and not force:
            print(
                f"gcae: keeping {state.run_id} (its merge is still recorded; pruning would "
                "break `gcae undo` for it — pass --force to delete it anyway)",
                file=sys.stderr,
            )
            continue
        lock = RunLock(runtime_dir, Path(state.source_repo))
        if lock.path.exists():
            try:
                lock.acquire()
            except RuntimeError as exc:
                print(
                    f"gcae: keeping {state.run_id} (its repository is locked: {exc})",
                    file=sys.stderr,
                )
                continue
            lock.release()
        stamp = state.updated_at.isoformat(timespec="seconds")
        if dry_run:
            print(f"would prune {state.run_id} ({state.status}, updated {stamp})")
        else:
            shutil.rmtree(run_dir)
            print(f"pruned {state.run_id} ({state.status}, updated {stamp})")
        pruned += 1
    if pruned:
        verb = "would be pruned" if dry_run else "pruned"
        print(f"{pruned} run record(s) {verb}")
    else:
        print("nothing to prune")


def _inspect_run(run_id: str, runtime_dir: Path, as_json: bool) -> None:
    state = StateStore(_state_path(runtime_dir, run_id)).load()
    if as_json:
        print(json.dumps(state.model_dump(mode="json"), indent=2, sort_keys=True))
        return
    print(f"run: {state.run_id} ({state.status}, {state.phase.value})")
    print(f"repository: {state.source_repo}")
    print(f"worktree: {state.worktree}")
    print(f"objective: {state.objective}")
    print(f"accepted: {state.accepted_steps} steps, commit {state.accepted_commit or 'none'}")
    print(f"iteration: {state.iteration}, current step: {state.current_step_id or 'none'}")
    if state.hard_constraints:
        print("constraints: " + "; ".join(state.hard_constraints))
    if state.success_criteria:
        print("criteria: " + "; ".join(state.success_criteria))
    for plan in state.plan:
        print(f"plan {plan.id} [{plan.status}]: {plan.goal}")
    if state.last_verification is not None:
        report = state.last_verification
        print(
            f"verification: passed={report.passed} hygiene={report.hygiene_passed} "
            f"criteria={sum(1 for item in report.criteria if item.passed)}/"
            f"{len(report.criteria)}"
        )
        for item in report.criteria:
            print(f"  [{'pass' if item.passed else 'fail'}] {item.criterion} :: {item.evidence}")
    if state.merge is not None:
        print(
            f"merged into {state.merge.target_branch} "
            f"({state.merge.pre_merge_commit[:12]} -> {state.merge.merge_commit[:12]})"
        )
    if state.pending_question:
        print(f"pending question: {state.pending_question}")
    if state.pending_input is not None:
        print(
            f"input required: {state.pending_input.prompt!r} "
            f"(from {state.pending_input.command}); answer with: "
            f"gcae input <repo> {state.run_id} <value>"
        )
    if state.blocked_reason:
        print(f"blocked: {state.blocked_reason}")
        if state.unblock_hint:
            print(f"unblocks with: {state.unblock_hint}")
    if state.degradations:
        print("degraded: " + "; ".join(state.degradations))
    if state.recovery is not None:
        print(
            f"recovery #{state.recovery.attempt} ({state.recovery.trigger}) "
            f"-> {state.recovery.strategy}"
        )
        print(f"  root cause: {state.recovery.root_cause}")
        print(f"  correction: {state.recovery.corrective_instruction}")


def _build_runtime(args: argparse.Namespace, config: Config, runtime_dir: Path) -> Runtime:
    base_provider, role_providers = _providers(config)
    control = RuntimeControl()
    return Runtime(
        args.repository,
        runtime_dir,
        worktree_dir=config.runtime.worktree_dir,
        provider=base_provider,
        validator_commands=config.validation.commands,
        max_steps=config.runtime.max_steps,
        recovery_attempts=config.runtime.recovery_attempts,
        command_idle_timeout=config.runtime.command_idle_timeout,
        command_startup_timeout=config.runtime.command_startup_timeout,
        strategy_retry_limit=config.runtime.strategy_retry_limit,
        require_execution_evidence=config.runtime.require_execution_evidence,
        recovery_budget=config.runtime.recovery_budget,
        command_timeout=config.runtime.command_timeout,
        context_limit=config.provider.context_limit,
        evaluator=_evaluator(config, role_providers.get("evaluator", base_provider)),
        planner=_planner(config, role_providers.get("planner", base_provider)),
        verifier=_verifier(config, role_providers.get("verifier", base_provider)),
        max_tool_calls_per_step=config.runtime.max_tool_calls_per_step,
        stagnation_window=config.runtime.stagnation_window,
        repetition_limit=config.runtime.repetition_limit,
        scope_warning_files=config.runtime.scope_warning_files,
        control=control,
        role_providers=role_providers,
        provider_label=config.provider.kind.upper(),
        auto_bootstrap=config.runtime.auto_bootstrap
        and not getattr(args, "no_auto_bootstrap", False),
        auto_merge=config.runtime.auto_merge,
        merge_accepted_on_failure=config.runtime.merge_accepted_on_failure,
        cleanup_after_merge=config.runtime.cleanup_after_merge,
        resolve_merge_conflicts=config.runtime.resolve_merge_conflicts,
    )


def _wants_tui(args: argparse.Namespace) -> bool:
    if getattr(args, "headless", False):
        return False
    if getattr(args, "tui", False):
        return True
    # a full request on the command line is a non-interactive instruction: run it
    if getattr(args, "request", None):
        return False
    return sys.stdout.isatty() and sys.stdin.isatty()


def _run_tui(
    runtime: Runtime,
    request: str | None = None,
    constraints: list[str] | None = None,
    criteria: list[str] | None = None,
) -> None:
    try:
        from .tui.app import GcaeApp
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError(
            "the TUI requires the 'textual' package; run 'pip install -e .' for this project"
        ) from exc

    GcaeApp(
        runtime,
        request=request,
        constraints=constraints,
        criteria=criteria,
    ).run()


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "request_flag", None) is not None:
        if args.request is not None:
            parser.error(
                "request given twice: pass the task either as the positional or as --request"
            )
        args.request = args.request_flag
    logging.basicConfig(level=logging.WARNING, format="gcae %(levelname)s: %(message)s")
    logging.getLogger("gcae").setLevel(logging.INFO)
    try:
        chosen = Path(args.config).expanduser() if args.config else discover_config()
        config = load_config(chosen)
        if chosen is not None:
            print(f"gcae: using config {chosen}", file=sys.stderr)
        elif config.provider.kind == "fake" and args.command in {"run", "resume"}:
            print(
                "gcae: no config file found (looked for ./config.toml and "
                "~/.config/gcae/config.toml) and the built-in default provider is the fake one, "
                "so the run will fail on its first model call — pass --config <file> or set "
                "GCAE_CONFIG",
                file=sys.stderr,
            )
        runtime_dir = (args.runtime_dir or config.state_dir).expanduser()
        if args.command == "prune":
            older_than = args.older_than
            if older_than is None:
                older_than = config.runtime.run_retention_days
            _prune_runs(runtime_dir, args.keep, args.dry_run, args.force, older_than)
            return
        if args.command == "input":
            runtime = _build_runtime(args, config, runtime_dir)
            runtime.resume(args.run_id, force=getattr(args, 'force', False))
            runtime.submit_process_input(args.text)
            result = runtime.run()
            print(_summary(result), file=sys.stderr)
            raise SystemExit(0 if result.status == "complete" else 1)
        if args.command == "undo":
            _undo(args.repository, args.run_id, runtime_dir)
            return
        if args.command == "merge":
            try:
                _merge_run(
                    args.repository,
                    args.run_id,
                    runtime_dir,
                    cleanup=config.runtime.cleanup_after_merge,
                )
            except MergeConflict as conflict:
                # a conflict is the agent's job here too, not the user's
                print(
                    f"gcae: merge conflicts in {', '.join(conflict.files)}; "
                    "handing them to the agent to resolve and re-verify",
                    file=sys.stderr,
                )
                resolver = _build_runtime(args, config, runtime_dir)
                resolver.resume(args.run_id)
                if resolver.resolve_merge_conflicts():
                    resolver.run()
                    _merge_run(
                        args.repository,
                        args.run_id,
                        runtime_dir,
                        cleanup=config.runtime.cleanup_after_merge,
                    )
                    return
                branch = resolver.state.branch if resolver.state is not None else args.run_id
                print(
                    f"gcae: conflicts remain in {', '.join(conflict.files)}; "
                    f"branch {branch} left intact for a later merge",
                    file=sys.stderr,
                )
                raise SystemExit(1) from conflict
            return
        if args.command == "list":
            _list_runs(runtime_dir)
            return
        if args.command == "inspect":
            _inspect_run(args.run_id, runtime_dir, args.json)
            return
        runtime = _build_runtime(args, config, runtime_dir)
        if _wants_tui(args):
            if args.command == "resume":
                runtime.resume(args.run_id, force=getattr(args, 'force', False))
                _run_tui(runtime)
            else:
                if args.request is None and not sys.stdin.isatty():
                    raise ValueError("a request is required when stdin is not a terminal")
                _run_tui(
                    runtime,
                    request=args.request,
                    constraints=args.constraint,
                    criteria=args.criterion,
                )
            if runtime.state is not None:
                print(_summary(runtime.state), file=sys.stderr)
            return
        if args.command == "run":
            if not args.request:
                raise ValueError("a request is required in headless mode")
            runtime.start(
                args.request,
                hard_constraints=args.constraint,
                success_criteria=args.criterion,
            )
        else:
            runtime.resume(args.run_id, force=getattr(args, 'force', False))
        result = runtime.run()
    except Exception as exc:  # noqa: BLE001 - a CLI must never dump a traceback on the user
        if isinstance(exc, (OSError, RuntimeError, ValueError)):
            print(f"gcae: error: {exc}", file=sys.stderr)
        else:
            print(f"gcae: unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
            print(
                "gcae: the run directory (state.json, events.jsonl) and any accepted commits are "
                "intact; 'gcae list' shows what exists and 'gcae resume' continues a run",
                file=sys.stderr,
            )
        raise SystemExit(1) from exc
    merge_options = {
        "auto_merge": config.runtime.auto_merge,
        "merge_accepted": config.runtime.merge_accepted_on_failure,
        "cleanup_after_merge": config.runtime.cleanup_after_merge,
    }
    merge_error: str | None = None
    if args.command in {"run", "resume"} and (
        result.status == "complete" or result.accepted_steps > 0
    ):
        try:
            _maybe_merge(
                result,
                runtime_dir,
                getattr(args, "merge", False),
                getattr(args, "no_merge", False),
                **merge_options,
            )
        except MergeConflict as conflict:
            print(
                f"gcae: merge conflicts in {', '.join(conflict.files)}; "
                "handing them to the agent to resolve and re-verify",
                file=sys.stderr,
            )
            handled = runtime.resolve_merge_conflicts()
            if handled:
                result = runtime.run()
                try:
                    _maybe_merge(
                        result,
                        runtime_dir,
                        getattr(args, "merge", False),
                        getattr(args, "no_merge", False),
                        **merge_options,
                    )
                except MergeConflict as still:
                    merge_error = ", ".join(still.files)
                    print(
                        f"gcae: conflicts remain in {merge_error}; "
                        f"branch {result.branch} left intact for `gcae merge`",
                        file=sys.stderr,
                    )
            else:
                merge_error = ", ".join(conflict.files)
                print(
                    f"gcae: conflicts remain in {merge_error}; "
                    f"branch {result.branch} left intact for `gcae merge`",
                    file=sys.stderr,
                )
    files: list[str] = []
    try:
        repo = GitRepository(result.source_repo, Path(runtime_dir).expanduser())
        files = _run_files(result, repo)
    except (GitError, OSError):
        validation = result.latest_validation
        files = list(validation.changed_files) if validation is not None else []
    result_path = Path("result.json")
    try:
        result_path.write_text(
            json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        )
    except OSError as exc:
        print(f"gcae: error: cannot write {result_path}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(file=sys.stderr)
    print(f"gcae: full result in {result_path}", file=sys.stderr)
    print(_summary(result, files), file=sys.stderr)
    if result.status != "complete":
        # a scripted caller must be able to tell an unfinished run from a finished one
        raise SystemExit(1)
    if merge_error:
        # the run finished but its work is not in the checkout: that is not a success
        raise SystemExit(1)
