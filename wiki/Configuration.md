# Configuration

One TOML file, read with `tomllib`. Every field below is implemented; there are no speculative
options. `config.example.toml` is the copy-ready template.

## Which file?

Without `--config`, GCAE looks at `$GCAE_CONFIG`, then `./config.toml`, then
`~/.config/gcae/config.toml`, and prints the file it used (`gcae: config config.toml`). If none
exists the built-in defaults apply — including the *fake* provider, and `run`/`resume` say so rather
than failing later with a confusing model error.

## Provider

```toml
[provider]
kind = "openrouter"                 # fake | http | openrouter
base_url = "https://openrouter.ai/api/v1"
model = "qwen/qwen3-coder"
api_key_env = "OPENROUTER_API_KEY"  # or api_key = "..." (keep that file out of Git)
timeout = 60.0                      # per-attempt HTTP timeout
context_limit = 8192                # budget for the reconstructed prompt
json_mode = true                    # ask for JSON output; false for models that reason better without
stream = true                       # stream completions (visible progress + stall detection)
stall_timeout = 45.0                # seconds of silence before a call is declared stalled
retries = 3                         # retries for 429 / 5xx / timeouts / dropped connections
retry_backoff = 2.0                 # base seconds, exponential; a Retry-After header wins

[provider.generation]
temperature = 0.0
max_tokens = 1024                   # reasoning models may need more (2048 for planner-heavy work)
```

## Roles

Any role can be pointed at its own model; omitted fields inherit `[provider]`:

```toml
[models.controller]
model = "qwen/qwen3-coder"

[models.planner]
model = "qwen/qwen3-coder"

[models.recovery]
model = "a-stronger-model"     # reads a failing trace and names the root cause

[models.escalation]
model = "a-stronger-model"     # fallback for a dead role, and for repeated failures
```

| Role | Used for |
| --- | --- |
| `controller` | every execution decision |
| `planner` | the initial plan and replanning |
| `evaluator` | progress judgement when `[evaluator] kind = "hybrid"` |
| `verifier` | the hybrid criteria judge |
| `escalation` | failover for a broken role and escalation on repeated failures |
| `recovery` | the self-diagnosis advisor (defaults to the controller) |

## Planner, evaluator, verifier

```toml
[planner]
kind = "auto"            # auto | llm | deterministic

[evaluator]
kind = "deterministic"   # deterministic | hybrid

[verifier]
kind = "deterministic"   # deterministic | hybrid
```

## Validation

```toml
[validation]
commands = ["python -m pytest -q"]   # run before a step can be accepted
```

## Runtime

```toml
[runtime]
state_dir = "~/.local/state/gcae"    # memory.db, runs/, worktrees/, locks/
worktree_dir = ""                    # optional: put worktrees elsewhere
max_steps = 20                       # iterations before the budget is exhausted
command_timeout = 30                 # seconds per run_command
max_tool_calls_per_step = 8          # then validation and evaluation are forced
stagnation_window = 3                # attempts without progress before the ladder
repetition_limit = 2                 # identical tool calls in a row before the garden is pruned
scope_warning_files = 10             # files touched before scope is worth reporting
recovery_attempts = 2                # self-diagnoses per run
recovery_budget = 5                  # extra iterations granted per successful correction
auto_bootstrap = true                # create the base commit a run needs
auto_merge = true                    # merge the verified branch on completion
resolve_merge_conflicts = true       # hand a conflicting merge to the agent
merge_accepted_on_failure = true     # a failed run may still deliver its accepted commits
cleanup_after_merge = true           # remove the worktree after a successful merge
```

## Precedence

1. command-line flags (`--config`, `--runtime-dir`, `--merge`, …)
2. the selected configuration file
3. built-in defaults

`api_key_env` is read from the environment at call time, so a key never has to be written into a
file that might be committed.
