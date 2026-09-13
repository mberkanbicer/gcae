from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import Config, load_config
from .http_provider import OpenAICompatibleProvider
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
    return parser


def _provider(config: Config) -> Provider:
    if config.provider.kind == "http":
        return OpenAICompatibleProvider(
            base_url=config.provider.base_url,
            model=config.provider.model,
            api_key=config.provider.api_key,
            api_key_env=config.provider.api_key_env,
            timeout=config.provider.timeout,
            context_limit=config.provider.context_limit,
            generation=config.provider.generation.model_dump(),
        )
    return FakeProvider(
        [{"action": "finish", "semantic_goal": "finish", "reason_summary": "fake provider"}]
    )


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    config = load_config(args.config)
    runtime_dir = args.runtime_dir or config.state_dir
    if args.command == "run":
        runtime = Runtime(
            args.repository,
            runtime_dir,
            worktree_dir=config.runtime.worktree_dir,
            provider=_provider(config),
            validator_commands=config.validation.commands,
            max_steps=config.runtime.max_steps,
            command_timeout=config.runtime.command_timeout,
        )
        runtime.start(
            args.request,
            hard_constraints=args.constraint,
            success_criteria=args.criterion,
        )
    else:
        runtime = Runtime(
            args.repository,
            runtime_dir,
            worktree_dir=config.runtime.worktree_dir,
            provider=_provider(config),
        )
        runtime.resume(args.run_id)
    result = runtime.run()
    print(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
