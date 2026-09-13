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

# dashboard keys: p pause · r resume · s stop · i instruct · d diff · l logs · m memory
#                  c context · e evaluation · t plan · Enter inspect · ? help · q quit

gcae resume ~/src/project <run-id>
gcae list
gcae inspect <run-id>
```

When a run completes, GCAE merges the verified branch into your current branch so the work is
visible in your checkout (`[runtime] auto_merge`, default on). The merge is recorded, so
`gcae undo <repo> <run-id>` puts your branch back; `--no-merge` keeps the branch separate for a
manual `gcae merge`. Everything git-side is handled by the loop: the base commit, the commit identity, the run
branch and worktree, the merge, and the cleanup of the worktree afterwards. A checkout with
uncommitted edits is committed as the base the merge builds on (never discarded), and a run that
failed *after* accepting checkpoints still hands that work over. A run that cannot be merged (a real
conflict, or a branch that moved) says so
and leaves the branch intact. A run that failed *after* accepting checkpoints can still be rescued:
`gcae merge <repo> <run-id>` (or `M` in the dashboard) merges that accepted work and tells you final
verification did not pass.

GCAE prepares the repository itself: if it has no commits yet, or the working tree has
uncommitted changes, GCAE creates the base commit the run needs (`.gitignore` respected, bounded to
2000 files / 50 MB, reported in the log and timeline) instead of refusing to start. File contents
are never modified. Pass `--no-auto-bootstrap` to keep refusal and commit manually. Non-repository
directories are still refused, and a repository mid-merge/rebase is left alone.

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
