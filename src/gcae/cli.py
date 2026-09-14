from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from .config import Config, ProviderConfig, load_config
from .evaluator import DeterministicEvaluator, Evaluator, LLMEvaluator
from .git import GitError, GitRepository, MergeConflict, NothingToMerge
from .http_provider import OpenAICompatibleProvider
from .models import AgentState, MergeRecord
from .persistence import StateStore
from .planner import LLMPlanner, Planner
from .providers import FakeProvider, Provider
from .runtime import (
    Runtime,
    RuntimeControl,
    cleanup_idle_worktree,
    cleanup_merged_worktree,
    merge_verified_run,
)
from .verifier import FinalVerifier

ROLES = ("controller", "planner", "evaluator", "verifier", "escalation", "recovery")


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
        sub.add_argument("--config", type=Path)
        sub.add_argument("--runtime-dir", type=Path)
        mode = sub.add_mutually_exclusive_group()
        sub.add_argument(
            "--no-auto-bootstrap",
            action="store_true",
            help="refuse to start when the repository needs a base commit instead of creating one",
        )
        mode.add_argument("--tui", action="store_true", help="force the interactive TUI")
        mode.add_argument("--headless", action="store_true", help="force non-interactive output")

    run = subparsers.add_parser("run")
    run.add_argument("repository", type=Path)
    run.add_argument(
        "request",
        nargs="?",
        default=None,
        help="task description; optional in TUI mode, where it is requested interactively",
    )
    add_runtime_flags(run)
    run.add_argument("--constraint", action="append", default=[])
    run.add_argument("--criterion", action="append", default=[])
    run.add_argument(
        "--merge", action="store_true", help="merge the verified branch without asking"
    )
    run.add_argument("--no-merge", action="store_true", help="never merge the run branch")

    resume = subparsers.add_parser("resume")
    resume.add_argument("repository", type=Path)
    resume.add_argument("run_id")
    add_runtime_flags(resume)

    listing = subparsers.add_parser("list", help="list known runs")
    listing.add_argument("--config", type=Path)
    listing.add_argument("--runtime-dir", type=Path)

    inspect = subparsers.add_parser("inspect", help="show a run summary")
    inspect.add_argument("run_id")
    inspect.add_argument("--config", type=Path)
    inspect.add_argument("--runtime-dir", type=Path)
    inspect.add_argument("--json", action="store_true", help="dump the full persisted state")

    undo = subparsers.add_parser("undo")
    undo.add_argument("repository", type=Path)
    undo.add_argument("run_id")
    undo.add_argument("--config", type=Path)
    undo.add_argument("--runtime-dir", type=Path)

    merge = subparsers.add_parser(
        "merge", help="merge a completed run branch into the current branch"
    )
    merge.add_argument("repository", type=Path)
    merge.add_argument("run_id")
    merge.add_argument("--config", type=Path)
    merge.add_argument("--runtime-dir", type=Path)
    return parser


def _provider(provider_config: ProviderConfig) -> Provider:
    kind = provider_config.kind.lower()
    if kind in {"http", "openrouter"}:
        local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
        api_key = provider_config.api_key or (
            os.environ.get(provider_config.api_key_env)
            if provider_config.api_key_env
            else None
        )
        host = urlparse(provider_config.base_url).hostname or ""
        if not api_key and host not in local_hosts:
            raise ValueError(
                f"no API key configured for {provider_config.base_url}; "
                "set provider.api_key or provider.api_key_env"
            )
        return OpenAICompatibleProvider(
            base_url=provider_config.base_url,
            model=provider_config.model,
            api_key=provider_config.api_key,
            api_key_env=provider_config.api_key_env,
            timeout=provider_config.timeout,
            context_limit=provider_config.context_limit,
            generation=provider_config.generation.model_dump(),
            json_mode=provider_config.json_mode,
            stream=provider_config.stream,
            stall_timeout=provider_config.stall_timeout,
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
            if config.provider.kind.lower() in {"http", "openrouter"}
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


def _summary(state: AgentState, files: list[str] | None = None) -> str:
    files = files or []
    verification = state.last_verification
    if verification is None:
        criteria = "no verification"
    else:
        passed = sum(1 for item in verification.criteria if item.passed)
        criteria = f"{passed}/{len(verification.criteria)} criteria passed"
    if state.merge is None and not files:
        branch = f"branch: {state.branch} (no file changes; nothing to merge)"
    elif state.merge is None:
        branch = (
            f"branch: {state.branch} (not merged yet — GCAE merges automatically; "
            f"run 'gcae merge {state.source_repo} {state.run_id}' if it stayed pending)"
        )
    else:
        branch = (
            f"branch: {state.branch} merged into {state.merge.target_branch} "
            f"(undo: gcae undo {state.source_repo} {state.run_id})"
        )
    if files:
        listing = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
        branch = f"{branch}\nfiles: {listing}"
    if not files:
        branch = f"{branch}\ndocuments: none — the run produced no files"
    elif state.merge is not None:
        branch = f"{branch}\ndocuments: {state.source_repo} (in your working tree now)"
    else:
        branch = (
            f"{branch}\ndocuments: {state.worktree} (worktree on branch {state.branch}; "
            "nothing is in your checkout until GCAE merges it)"
        )
    question = ""
    if state.pending_question:
        question = (
            f"\nquestion: {state.pending_question}"
            f"\nanswer with: gcae resume {state.source_repo} {state.run_id} "
            "(or press i in the dashboard)"
        )
    return (
        f"run {state.run_id}: {state.status}\n"
        f"accepted steps: {state.accepted_steps}, commit: {state.accepted_commit or 'none'}\n"
        f"verification: {criteria}\n"
        f"worktree: {state.worktree}\n"
        f"{branch}{question}"
    )


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
    print(f"{'run id':<14} {'status':<18} {'steps':>5}  {'updated':<20} objective")
    for state in rows:
        objective = state.objective.replace("\n", " ")[:60]
        print(
            f"{state.run_id:<14} {state.status:<18} {state.accepted_steps:>5}  "
            f"{state.updated_at.isoformat(timespec='seconds'):<20} {objective}"
        )


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
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("gcae").setLevel(logging.INFO)
    try:
        config = load_config(args.config)
        runtime_dir = (args.runtime_dir or config.state_dir).expanduser()
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
                runtime.resume(args.run_id)
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
            runtime.resume(args.run_id)
        result = runtime.run()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"gcae: error: {exc}", file=sys.stderr)
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
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    print(_summary(result, files), file=sys.stderr)
    if result.status != "complete":
        # a scripted caller must be able to tell an unfinished run from a finished one
        raise SystemExit(1)
    if merge_error:
        # the run finished but its work is not in the checkout: that is not a success
        raise SystemExit(1)
