# Configuration

One TOML file, read with `tomllib`. Every field below is implemented; there are no speculative
options. `config.example.toml` is the copy-ready template.

**Which file?** Without `--config`, GCAE looks at `$GCAE_CONFIG`, then `./config.toml`, then
`~/.config/gcae/config.toml`, and prints the one it used (`gcae: using config config.toml`). If none of
them exists the built-in defaults apply — including the *fake* provider, and `run`/`resume` say so
rather than failing later with a confusing model error.

```toml
[runtime]
state_dir = "~/.local/state/gcae"   # memory.db, runs/, worktrees/
worktree_dir = "~/.local/state/gcae/worktrees"  # optional override
auto_bootstrap = true               # create the base commit a run needs (unborn or dirty repo)
auto_merge = true                   # merge the verified branch on completion (--no-merge overrides)
resolve_merge_conflicts = true      # hand a conflicting merge to the agent, re-verify, retry
merge_accepted_on_failure = true    # merge the checkpoints a failed/stopped run accepted
cleanup_after_merge = true          # remove GCAE's worktree once merged (the branch is kept)
command_idle_timeout = 20.0            # no output while running -> stalled command
command_startup_timeout = 10.0         # no output at all -> command never really started
strategy_retry_limit = 2               # identical failures before a blind repeat is refused
require_execution_evidence = true      # code changed + runnable check declared => a command must run
recovery_attempts = 2               # self-diagnoses per run before the run must ask the user
recovery_budget = 5                 # extra iterations granted by each successful correction
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
timeout = 60                        # per-request budget
context_limit = 8192                # token budget for reconstructed context
json_mode = true                    # response_format=json_object; false for reasoning models
stream = true                       # stream completions: visible progress + stall detection
stall_timeout = 45.0                # seconds with no data before a call is declared stalled
retries = 3                         # retries for 429/5xx/timeouts/dropped connections
retry_backoff = 2.0                 # base seconds, exponential; Retry-After wins

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
[models.recovery]
model = "anthropic/claude-sonnet-4"

[planner]
kind = "auto"                       # auto | llm | deterministic

[evaluator]
kind = "deterministic"              # deterministic | llm

[verifier]
kind = "deterministic"              # deterministic | hybrid

[validation]
commands = ["pytest -q"]            # run before every semantic evaluation

[sandbox]
command_prefix = []                 # optional wrapper around every executed command, e.g.
# command_prefix = ["bwrap", "--ro-bind", "/", "/", "--bind", "{worktree}", "{worktree}",
#                   "--dev", "/dev", "--proc", "/proc", "--unshare-all", "--"]
# `{worktree}` and `{repo}` are replaced with the run worktree and the source repository.
```

## Behavior notes

- `planner.kind = "auto"` uses the model for planning when `provider.kind` is `http`/`openrouter`,
  and the deterministic planner otherwise. The planner derives success criteria from the request
  (at least one is required when no `--criterion` is supplied); planned criteria and constraints
  are merged with the user's, and user-provided entries are never overwritten and always stored as
  immutable memory.
- `evaluator.kind = "llm"` sends the reconstructed context and deterministic validation evidence
  to the configured model; it can promote failure memories before a rollback.
- `verifier.kind = "hybrid"` keeps all deterministic checks and additionally asks the configured
  verifier model to judge criteria that have no deterministic form. The judge receives the
  objective, accepted commit, changed files, validation command results, a truncated diff and
  bounded worktree file samples; it must return `{passed, evidence}` and any provider error, empty
  evidence or `passed = false` fails verification. `deterministic` (the default) fails such
  criteria closed instead of guessing.
- `models.<role>` overrides create a dedicated provider for that role without changing the base
  provider. `models.escalation` is used for controller decisions after two consecutive rejected
  steps; `models.recovery` answers the self-diagnosis that reads a failing run's trace (it defaults
  to the controller's model, which is the escalated one after escalation); `models.verifier` is used
  by hybrid verification. Each role is only created when configured.
- `stream` and `stall_timeout` decide how a model call behaves while it runs: streamed completions
  report progress (and reasoning characters) as events, and a call that produces nothing for
  `stall_timeout` seconds fails with `provider request stalled: …` instead of holding the run. A
  buffered request gets the same stall budget, and an endpoint that refuses streaming falls back to a
  buffered request automatically — a timeout never does, because a silent endpoint would be silent
  again.
- `retries` and `retry_backoff` cover transient provider failures: 429 and 5xx responses,
  timeouts and dropped connections are retried with exponential backoff plus jitter, honouring a
  `Retry-After` header. Client errors (400/404/422) are not retried — they are the caller's
  problem, and the run's recovery ladder handles them.
- `command_idle_timeout`, `command_startup_timeout` and `strategy_retry_limit` shape how execution
  failures are detected and how quickly a blind repeat is refused; `require_execution_evidence`
  decides whether an acceptance without a single executed command is allowed when the run declares a
  runnable check.
- `recovery_attempts` and `recovery_budget` bound self-recovery: each diagnosis may queue a corrective
  step and buy extra iterations, and the run asks the user only when those attempts are spent.
- `context_limit` is a token budget; the runtime estimates tokens conservatively
  (`(characters + 3) // 4`). Pinned information may exceed the budget rather than be dropped.
- `validation.commands` are executed with the isolated worktree as cwd and count as validation
  evidence; failures fail the step. The `run_tests` tool runs the same commands.
- `sandbox.command_prefix` wraps every command the run executes — step commands, validation
  commands, `command succeeds:` criteria and `run_tests`. The blocklist still judges the raw
  command first. GCAE only routes commands through the wrapper; whether the wrapper actually
  isolates (mounts, network, namespaces) is the operator's configuration. Interactive and
  scripted-input commands keep working if the wrapper forwards stdin.
- API keys may be inline (`api_key`) or read from the environment (`api_key_env`). Keep
  `config.toml` out of repositories; it is gitignored here.
