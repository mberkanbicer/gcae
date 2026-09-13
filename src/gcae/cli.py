from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .config import Config, load_config
from .evaluator import DeterministicEvaluator, Evaluator, LLMEvaluator
from .http_provider import OpenAICompatibleProvider
from .models import AgentState
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
    resume = subparsers.add_parser("resume")
    resume.add_argument("repository", type=Path)
    resume.add_argument("run_id")
    resume.add_argument("--config", type=Path)
    resume.add_argument("--runtime-dir", type=Path)
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
        f"worktree: {state.worktree}"
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("gcae").setLevel(logging.INFO)
    config = load_config(args.config)
    runtime_dir = args.runtime_dir or config.state_dir
    try:
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
    except (RuntimeError, ValueError) as exc:
        print(f"gcae: error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    print(_summary(result), file=sys.stderr)
