# Configuration

One TOML file, read with `tomllib`. Every field below is implemented; there are no speculative
options. `config.example.toml` is the copy-ready template.

```toml
[runtime]
state_dir = "~/.local/state/gcae"   # memory.db, runs/, worktrees/
worktree_dir = "~/.local/state/gcae/worktrees"  # optional override
max_steps = 20                      # outer loop bound per run
command_timeout = 30                # seconds per command tool call
max_tool_calls_per_step = 8         # forces step evaluation
stagnation_window = 3               # consecutive non-progress iterations
repetition_limit = 2                # identical tool calls before replan
scope_warning_files = 10            # soft changed-file warning threshold

[provider]
kind = "http"                       # fake | http | openrouter (alias of http)
base_url = "https://openrouter.ai/api/v1"
model = "openai/gpt-4o-mini"
api_key_env = "OPENROUTER_API_KEY"  # or api_key = "..."
timeout = 60
context_limit = 8192                # token budget for reconstructed context

[provider.generation]
temperature = 0.0
max_tokens = 1024

# Optional stronger model for planning, evaluation, verification or escalation.
# Any field omitted is inherited from [provider].
[models.planner]
model = "qwen3:32b"
[models.evaluator]
model = "qwen3:32b"
[models.escalation]
model = "anthropic/claude-sonnet-4"

[planner]
kind = "auto"                       # auto | llm | deterministic

[evaluator]
kind = "deterministic"              # deterministic | llm

[validation]
commands = ["pytest -q"]            # run before every semantic evaluation
```

## Behavior notes

- `planner.kind = "auto"` uses the model for planning when `provider.kind` is `http`/`openrouter`,
  and the deterministic planner otherwise. Planned criteria and constraints are merged with the
  user's; user-provided entries are never overwritten and always stored as immutable memory.
- `evaluator.kind = "llm"` sends the reconstructed context and deterministic validation evidence
  to the configured model; it can promote failure memories before a rollback.
- `models.<role>` overrides create a dedicated provider for that role without changing the base
  provider. `models.escalation` is used for controller decisions after two consecutive rejected
  steps, and is only created when configured.
- `context_limit` is a token budget; the runtime estimates tokens conservatively
  (`(characters + 3) // 4`). Pinned information may exceed the budget rather than be dropped.
- `validation.commands` are executed with the isolated worktree as cwd and count as validation
  evidence; failures fail the step. The `run_tests` tool runs the same commands.
- API keys may be inline (`api_key`) or read from the environment (`api_key_env`). Keep
  `config.toml` out of repositories; it is gitignored here.
