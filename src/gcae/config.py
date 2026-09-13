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


class RuntimeConfig(BaseModel):
    state_dir: str = Field(default_factory=lambda: _default_state_dir())
    worktree_dir: str | None = None
    # GCAE creates the base commit a run needs (unborn HEAD, dirty tree) instead of
    # refusing to start. Never touches file contents; bounded and reported.
    auto_bootstrap: bool = True
    # Merge the verified run branch into the source branch when a run completes, so the
    # work is visible in the user's checkout. Recorded in state.json; `gcae undo` reverses.
    auto_merge: bool = True
    max_steps: int = 20
    command_timeout: int = 30
    max_tool_calls_per_step: int = 8
    stagnation_window: int = 3
    repetition_limit: int = 2
    scope_warning_files: int = 10


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


def load_config(path: str | Path | None = None) -> Config:
    if path is None:
        return Config()
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")
    with config_path.open("rb") as handle:
        try:
            data: dict[str, Any] = tomllib.load(handle)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"invalid TOML in {config_path}: {exc}") from exc
    return Config.model_validate(data)
