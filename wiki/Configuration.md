# Configuration

`gcae` reads TOML: pass `--config PATH`, or place it at `~/.config/gcae/config.toml`. Start from
[`config.example.toml`](https://github.com/mberkanbicer/gcae/blob/main/config.example.toml).
`config.toml` is gitignored because it usually holds an API key.

## Provider

| Key | Default | Meaning |
| --- | --- | --- |
| `provider.kind` | `fake` | `http`, `openrouter`, `fake` (deterministic, offline) |
| `provider.base_url` | – | OpenAI-compatible endpoint |
| `provider.model` | – | model name sent to the endpoint |
| `provider.api_key` / `api_key_env` | – | inline key or the environment variable holding it; local endpoints need neither |
| `provider.timeout` | `60` | request timeout in seconds |
| `provider.context_limit` | `8192` | token budget for the reconstructed context, and the default output cap |
| `provider.json_mode` | `true` | send `response_format=json_object`; disable for reasoning models |
| `provider.generation` | `{}` | merged verbatim into the request body (`temperature`, `max_tokens`, provider-specific knobs) |

## Roles

Different models per role, all optional:

```toml
[models.escalation]                 # used after repeated failures
model = "a/stronger-model"

[models.planner]
model = "a/planner-model"
```

Roles: `controller`, `planner`, `evaluator`, `verifier`, `escalation`.

## Planner, evaluator, verifier

| Key | Default | Meaning |
| --- | --- | --- |
| `planner.kind` | `auto` | `auto` (model for HTTP providers), `llm`, `deterministic` |
| `evaluator.kind` | `deterministic` | `deterministic` or `llm` |
| `verifier.kind` | `deterministic` | `deterministic`, or `hybrid` to allow a strict model judge |

## Validation

```toml
[validation]
commands = ["pytest -q", "ruff check ."]
```

Run at every evaluation; their results are the deterministic evidence the evaluator and the final
verifier see.

## Runtime

| Key | Default | Meaning |
| --- | --- | --- |
| `runtime.state_dir` | XDG state dir | where runs, memory and worktrees live |
| `runtime.worktree_dir` | `<state_dir>/worktrees` | where run worktrees are created |
| `runtime.max_steps` | `20` | iterations before a run gives up |
| `runtime.command_timeout` | `30` | seconds per `run_command` |
| `runtime.max_tool_calls_per_step` | `8` | tool calls before a step is forced to evaluation |
| `runtime.stagnation_window` | `3` | non-productive attempts before the run asks the user |
| `runtime.repetition_limit` | `2` | identical calls allowed before evaluation is forced |
| `runtime.scope_warning_files` | `10` | changed files above which a scope warning is raised |
| `runtime.auto_bootstrap` | `true` | create the base commit a run needs |
| `runtime.auto_merge` | `true` | merge the verified branch on completion |
| `runtime.merge_accepted_on_failure` | `true` | hand over checkpoints a failed run accepted |
| `runtime.resolve_merge_conflicts` | `true` | resolve merge conflicts through the agent |
| `runtime.cleanup_after_merge` | `true` | remove GCAE's worktree once merged (branch kept) |

## Precedence

Command-line flags beat configuration, which beats defaults. `--runtime-dir` overrides
`runtime.state_dir`; `--no-merge` beats `auto_merge`; `--no-auto-bootstrap` beats `auto_bootstrap`.
