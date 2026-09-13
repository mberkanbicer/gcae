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


class RuntimeConfig(BaseModel):
    state_dir: str = Field(default_factory=lambda: _default_state_dir())
    worktree_dir: str | None = None
    max_steps: int = 20
    command_timeout: int = 30


class ValidationConfig(BaseModel):
    commands: list[str] = Field(default_factory=list)


class EvaluatorConfig(BaseModel):
    kind: str = "deterministic"


class Config(BaseModel):
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    evaluator: EvaluatorConfig = Field(default_factory=EvaluatorConfig)
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
    with Path(path).open("rb") as handle:
        data: dict[str, Any] = tomllib.load(handle)
    return Config.model_validate(data)
