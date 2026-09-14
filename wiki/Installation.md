# Installation

## Requirements

| Requirement | Detail |
| --- | --- |
| Python | 3.12 or newer |
| Git | any recent version; GCAE drives `git` itself |
| A model | an OpenAI-compatible endpoint: OpenRouter, a local Ollama/LM Studio server, or any HTTP provider that speaks `POST /chat/completions` |
| OS | Linux (developed and tested on Linux/WSL); the repository lock uses `fcntl` |

Runtime dependencies are exactly `pydantic`, `httpx` and `textual`. There is no vector database, no
embedding model, no message broker, no agent framework.

## Install from a release

```bash
python -m venv .venv
.venv/bin/pip install gcae-0.1.7-py3-none-any.whl    # from the Releases page
```

## Install from the repository

```bash
git clone https://github.com/mberkanbicer/gcae.git
cd gcae
python -m venv .venv
.venv/bin/pip install -e .
.venv/bin/gcae --version
```

## Verify the installation

```bash
.venv/bin/gcae --version     # prints the installed version
.venv/bin/gcae list          # prints "no runs found" on a fresh state directory
.venv/bin/pytest -q          # optional: the full test suite
```

## Where GCAE keeps its state

Everything the runtime owns lives outside your repositories, under
`${XDG_STATE_HOME:-~/.local/state}/gcae`:

```
state.json            per-run state (checkpoint, plan, verification, degradations, recovery)
events.jsonl          append-only event log — the trace self-diagnosis reads
memory.db             SQLite memory (facts, decisions, failure lessons) that survives rollback
worktrees/<run>/      the isolated worktree of a run
locks/<digest>.lock    one run per source repository
```

Nothing is written into the repository you point GCAE at, except the commits the run accepted and
the recorded merge.

## Uninstall

```bash
.venv/bin/pip uninstall gcae
rm -r "${XDG_STATE_HOME:-$HOME/.local/state}/gcae"   # optional: history, memory and worktrees
```
