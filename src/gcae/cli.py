from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, load_config
from .evaluator import DeterministicEvaluator, Evaluator, LLMEvaluator
from .git import GitError, GitRepository
from .http_provider import OpenAICompatibleProvider
from .models import AgentState, MergeRecord
from .persistence import StateStore
from .providers import FakeProvider, Provider
from .runtime import Runtime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gcae", description="Git-Checkpointed Adaptive Execution")
    parser.add_argument("--version", action="version", version="0.1.0")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("repository", type=Path)
    run.add_argument("request")
    run.add_argument("--config", type=Path)
    run.add_argument("--runtime-dir", type=Path)
    run.add_argument("--constraint", action="append", default=[])
    run.add_argument("--criterion", action="append", default=[])
    run.add_argument(
        "--merge",
        action="store_true",
        help="merge the verified run branch without asking",
    )
    run.add_argument(
        "--no-merge",
        action="store_true",
        help="never merge the run branch",
    )
    resume = subparsers.add_parser("resume")
    resume.add_argument("repository", type=Path)
    resume.add_argument("run_id")
    resume.add_argument("--config", type=Path)
    resume.add_argument("--runtime-dir", type=Path)
    undo = subparsers.add_parser("undo")
    undo.add_argument("repository", type=Path)
    undo.add_argument("run_id")
    undo.add_argument("--config", type=Path)
    undo.add_argument("--runtime-dir", type=Path)
    return parser


def _provider(config: Config) -> Provider:
    kind = config.provider.kind.lower()
    if kind in {"http", "openrouter"}:
        return OpenAICompatibleProvider(
            base_url=config.provider.base_url,
            model=config.provider.model,
            api_key=config.provider.api_key,
            api_key_env=config.provider.api_key_env,
            timeout=config.provider.timeout,
            context_limit=config.provider.context_limit,
            generation=config.provider.generation.model_dump(),
        )
    if kind == "fake":
        return FakeProvider(
            [{"action": "finish", "semantic_goal": "finish", "reason_summary": "fake provider"}]
        )
    raise ValueError(f"unsupported provider kind: {config.provider.kind!r}")


def _evaluator(config: Config, provider: Provider) -> Evaluator:
    kind = config.evaluator.kind.lower()
    if kind == "deterministic":
        return DeterministicEvaluator()
    if kind == "llm":
        return LLMEvaluator(provider)
    raise ValueError(f"unsupported evaluator kind: {config.evaluator.kind!r}")


def _summary(state: AgentState) -> str:
    verification = state.last_verification
    if verification is None:
        criteria = "no verification"
    else:
        passed = sum(1 for item in verification.criteria if item.passed)
        criteria = f"{passed}/{len(verification.criteria)} criteria passed"
    return (
        f"run {state.run_id}: {state.status}\n"
        f"accepted steps: {state.accepted_steps}, commit: {state.accepted_commit or 'none'}\n"
        f"verification: {criteria}\n"
        f"worktree: {state.worktree}\n"
        f"branch: {state.branch} (not merged; apply with: git merge {state.branch})"
    )


def _undo(repository: Path, run_id: str, runtime_dir: Path) -> None:
    state_path = Path(runtime_dir).expanduser() / "runs" / run_id / "state.json"
    state = StateStore(state_path).load()
    if Path(state.source_repo).resolve() != Path(repository).resolve():
        raise RuntimeError("undo repository does not match persisted state")
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


def _maybe_merge(
    result: AgentState,
    runtime_dir: Path,
    merge_flag: bool,
    no_merge_flag: bool,
) -> None:
    if no_merge_flag or result.merge is not None or not result.branch:
        return
    if merge_flag:
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
            f"gcae: branch {result.branch} is ready; merge manually or rerun with --merge",
            file=sys.stderr,
        )
        return
    if not approved:
        return
    repo = GitRepository(result.source_repo, Path(runtime_dir).expanduser())
    try:
        target = repo.current_branch()
        pre, merged = repo.merge_branch(result.branch)
    except GitError as exc:
        print(f"gcae: merge skipped: {exc}", file=sys.stderr)
        return
    result.merge = MergeRecord(
        branch=result.branch,
        target_branch=target,
        pre_merge_commit=pre,
        merge_commit=merged,
    )
    StateStore(
        Path(runtime_dir).expanduser() / "runs" / result.run_id / "state.json"
    ).save(result)
    print(
        f"gcae: merged {result.branch} into {target} ({pre[:12]} -> {merged[:12]})",
        file=sys.stderr,
    )
    print(f"gcae: undo with: gcae undo {result.source_repo} {result.run_id}", file=sys.stderr)


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
        provider = _provider(config)
        runtime = Runtime(
            args.repository,
            runtime_dir,
            worktree_dir=config.runtime.worktree_dir,
            provider=provider,
            validator_commands=config.validation.commands,
            max_steps=config.runtime.max_steps,
            command_timeout=config.runtime.command_timeout,
            context_limit=config.provider.context_limit,
            evaluator=_evaluator(config, provider),
        )
        if args.command == "run":
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
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    print(_summary(result), file=sys.stderr)
    if args.command == "run" and result.status == "complete":
        _maybe_merge(result, runtime_dir, args.merge, args.no_merge)
