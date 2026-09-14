from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class GenerationConfig(BaseModel):
    temperature: float = 0.0
    max_tokens: int = 1024


class ProviderConfig(BaseModel):
    kind: str = "fake"
    base_url: str = "http://localhost:11434/v1"
    model: str = "llama3.2"
    api_key: str | None = None
    api_key_env: str | None = None
    timeout: float = 60.0
    context_limit: int = 8192
    generation: GenerationConfig = Field(default_factory=GenerationConfig)
    # response_format=json_object. Disable for reasoning models that deliberate until the
    # output budget is exhausted and never emit content.
    json_mode: bool = True
    # Stream the completion: the dashboard and the event log see tokens as they arrive, and
    # a hang becomes a detected stall instead of an indefinite wait. Falls back to a buffered
    # request automatically when the endpoint refuses streaming.
    stream: bool = True
    # Seconds without any streamed data before the call is declared stalled and handed to the
    # recovery ladder.
    stall_timeout: float = 45.0
    # Transient provider failures (rate limits, server errors, dropped connections) are retried
    # with exponential backoff before they can fail a step.
    retries: int = 3
    retry_backoff: float = 2.0


class ModelOverride(BaseModel):
    """Optional per-role overrides on top of the default provider."""

    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    timeout: float | None = None
    context_limit: int | None = None

    def resolved(self, base: ProviderConfig) -> ProviderConfig | None:
        overrides = self.model_dump(exclude_none=True)
        if not overrides:
            return None
        return base.model_copy(update=overrides)


class ModelsConfig(BaseModel):
    controller: ModelOverride = Field(default_factory=ModelOverride)
    planner: ModelOverride = Field(default_factory=ModelOverride)
    evaluator: ModelOverride = Field(default_factory=ModelOverride)
    verifier: ModelOverride = Field(default_factory=ModelOverride)
    escalation: ModelOverride = Field(default_factory=ModelOverride)
    # model that diagnoses a failing run from its own trace (defaults to controller)
    recovery: ModelOverride = Field(default_factory=ModelOverride)


class RuntimeConfig(BaseModel):
    state_dir: str = Field(default_factory=lambda: _default_state_dir())
    worktree_dir: str | None = None
    # GCAE creates the base commit a run needs (unborn HEAD, dirty tree) instead of
    # refusing to start. Never touches file contents; bounded and reported.
    auto_bootstrap: bool = True
    # Merge the verified run branch into the source branch when a run completes, so the
    # work is visible in the user's checkout. Recorded in state.json; `gcae undo` reverses.
    auto_merge: bool = True
    # A conflicting merge is handed to the agent, which resolves it in the run worktree,
    # re-verifies, and the merge is retried.
    resolve_merge_conflicts: bool = True
    # Rescue checkpoints from a run that failed or was stopped without final verification.
    merge_accepted_on_failure: bool = True
    # Remove GCAE's own worktree once its branch is merged (the branch is kept, so
    # gcae undo can still reverse the merge and the work can be re-merged).
    cleanup_after_merge: bool = True
    max_steps: int = 20
    command_timeout: int = 30
    max_tool_calls_per_step: int = 8
    stagnation_window: int = 3
    repetition_limit: int = 2
    scope_warning_files: int = 10
    # A run that is about to fail reads its own trace and tries a correction before it
    # asks the user: how many self-diagnoses per run, and how many extra iterations each
    # successful correction buys.
    recovery_attempts: int = 2
    recovery_budget: int = 5


class ValidationConfig(BaseModel):
    commands: list[str] = Field(default_factory=list)


class EvaluatorConfig(BaseModel):
    kind: str = "deterministic"


class VerifierConfig(BaseModel):
    kind: str = "deterministic"  # deterministic | hybrid


class PlannerConfig(BaseModel):
    kind: str = "auto"


class Config(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    models: ModelsConfig = Field(default_factory=ModelsConfig)
    planner: PlannerConfig = Field(default_factory=PlannerConfig)
    evaluator: EvaluatorConfig = Field(default_factory=EvaluatorConfig)
    verifier: VerifierConfig = Field(default_factory=VerifierConfig)
    validation: ValidationConfig = Field(default_factory=ValidationConfig)

    @property
    def state_dir(self) -> Path:
        return Path(os.path.expanduser(self.runtime.state_dir))


def _default_state_dir() -> str:
    """Return the XDG-compatible default without expanding it at import time."""
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return str(Path(state_home) / "gcae")
    return "~/.local/state/gcae"


#: Where a config file is looked for when the caller does not name one.  Without this the
#: runtime would silently fall back to the built-in defaults (a fake provider) and fail on the
#: first model call, which looks like a bug in the model rather than a missing file.
CONFIG_DISCOVERY = (
    Path("config.toml"),
    Path("~/.config/gcae/config.toml"),
)


def discover_config() -> Path | None:
    """The config file to use when none was named: ``$GCAE_CONFIG``, ``./config.toml``,
    ``~/.config/gcae/config.toml``."""
    env = os.environ.get("GCAE_CONFIG")
    candidates = [Path(env).expanduser()] if env else []
    candidates.extend(item.expanduser() for item in CONFIG_DISCOVERY)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        discovered = discover_config()
        if discovered is None:
            return Config()
        path = discovered
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("rb") as handle:
        try:
            data: dict[str, Any] = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid TOML in {config_path}: {exc}") from exc
    return Config.model_validate(data)
