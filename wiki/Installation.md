# Installation

## Requirements

| Requirement | Notes |
| --- | --- |
| Python 3.12 or newer | CI tests 3.12 and 3.13 |
| Git | any recent version; GCAE shells out to `git` |
| A model provider | a local Ollama endpoint needs no API key; OpenRouter or any OpenAI-compatible API also works |

## Install from the repository

The package is not published on PyPI yet.

```bash
git clone https://github.com/mberkanbicer/gcae.git
cd gcae
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"     # runtime + test/lint tooling
.venv/bin/gcae --version
```

Use `pip install -e .` instead if you do not want the development tools (pytest, ruff, mypy, build).

## Verify the installation

```bash
.venv/bin/gcae --version          # 0.1.0
.venv/bin/gcae --help
.venv/bin/pytest -q               # 170 tests, no network needed
```

The test suite uses deterministic providers and `httpx.MockTransport`; it never calls a real model
and never needs an API key.

## Where GCAE keeps its state

Everything GCAE writes lives outside your repositories, under
`${XDG_STATE_HOME:-~/.local/state}/gcae`:

```
~/.local/state/gcae/
├── memory.db                 SQLite FTS5 memory, cumulative across runs
├── runs/<run-id>/            state.json, events.jsonl, diffs/, tool-results/
└── worktrees/<run-id>        one isolated worktree per run
```

`[runtime] state_dir` and `[runtime] worktree_dir` move these. The state directory must be writable;
GCAE reports a clear error when it is not.

## Uninstall

```bash
.venv/bin/python -m pip uninstall gcae
```

Removing `~/.local/state/gcae` deletes all run history and memory. GCAE never stores anything inside
the repositories it works on, apart from the commits it creates and merges.
