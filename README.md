# GCAE — Git-Checkpointed Adaptive Execution

GCAE is a lightweight, single-agent execution runtime for coding tasks. It plans a request, works
in semantic steps inside one isolated Git worktree, validates each step deterministically,
evaluates whether it advanced the objective, checkpoints accepted work, rolls back rejected work,
keeps failure knowledge in SQLite, and verifies every success criterion before declaring
completion. It runs headless or with an interactive TUI.

## Install

```bash
python -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/gcae --version
```

## Run

```bash
# interactive TUI on a terminal; it asks for the task, the planner derives criteria,
# and verified work ends on branch gcae/<run-id>
gcae run ~/src/project --config ~/.config/gcae/config.toml

# or give the task up front
gcae run ~/src/project "Add a --dry-run flag to the importer" \
  --criterion "command succeeds: pytest -q" \
  --config ~/.config/gcae/config.toml

# non-interactive: state JSON on stdout, logs on stderr
gcae run ~/src/project "Add a --dry-run flag" --headless

gcae resume ~/src/project <run-id>
gcae list
gcae inspect <run-id>
```

The target repository must be clean and committed. GCAE never modifies your working tree; a dirty
repository is refused. After a successful run it asks whether to merge the verified branch
(`--merge` / `--no-merge`), records the pre-merge commit, and `gcae undo` reverses a merge. See
[`docs/CLI.md`](docs/CLI.md).

## Configuration

One small TOML file: provider (OpenRouter, Ollama, vLLM, LM Studio — any OpenAI-compatible
endpoint), optional per-role models, planner/evaluator selection, context budget, step budgets and
validation commands. See [`docs/CONFIGURATION.md`](docs/CONFIGURATION.md) and
[`config.example.toml`](config.example.toml).

```bash
cp config.example.toml ~/.config/gcae/config.toml   # then edit; never commit API keys
```

## Docs

| Topic | File |
| --- | --- |
| Architecture, semantic-step loop | `docs/ARCHITECTURE.md` |
| Phases, actions, run status | `docs/STATE_MACHINE.md` |
| Worktrees, checkpoints, merge/undo | `docs/GIT_EXECUTION.md` |
| Memory, working memory, context budgeting | `docs/MEMORY_CONTEXT.md` |
| Tools, sandboxing, command guardrails | `docs/TOOLS.md` |
| Validation, evaluation, replanning | `docs/ARCHITECTURE.md`, `docs/WORKSPACE_HYGIENE.md` |
| Providers and role models | `docs/PROVIDERS.md` |
| CLI reference | `docs/CLI.md` |
| TUI | `docs/TUI.md` |
| Configuration reference | `docs/CONFIGURATION.md` |
| Audit of the pre-takeover code | `docs/IMPLEMENTATION_AUDIT.md` |
| Test architecture | `docs/TEST_PLAN.md` |

## Checks

```bash
.venv/bin/pytest -q        # includes the mandatory rollback, context-budget and TUI tests
.venv/bin/ruff check .
.venv/bin/mypy src/gcae
```
