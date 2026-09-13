# GCAE — Git-Checkpointed Adaptive Execution

GCAE is a small, explicit Python runtime for reversible coding tasks. It uses one external Git worktree per run, immutable SQLite memory with FTS5 retrieval, deterministic validation, structured Pydantic decisions, and bounded replanning.

## Install and run

```bash
python -m venv .venv
.venv/bin/python -m pip install -e . pytest ruff mypy
.venv/bin/python -m gcae --help
```

Run against a committed repository (the source tree is never modified):

```bash
.venv/bin/python -m gcae run /path/to/repository "Add a feature" --runtime-dir ~/.local/state/gcae
```

Use deterministic completion criteria when possible:

```bash
.venv/bin/python -m gcae run /path/to/repository "Add a feature" \
  --criterion "file exists: src/example.py" \
  --criterion "command succeeds: pytest -q"
```

See `config.example.toml` and `docs/` for the implemented contracts. `context_limit` is the
token budget used when the runtime reconstructs controller context (and the request's maximum
output tokens for the HTTP provider). The evaluator defaults to deterministic validation rules;
`[evaluator] kind = "llm"` asks the configured model to evaluate each step instead.

Resume an interrupted run (waiting for user input or interrupted mid-step):

```bash
.venv/bin/python -m gcae resume /path/to/repository <run-id> --runtime-dir ~/.local/state/gcae
```

Each run prints the final `AgentState` as JSON on stdout and a short summary on stderr. The
verified branch `gcae/<run-id>` and its worktree are left in place for inspection; the runtime
never merges them into the source branch.

## Provider examples

Local Ollama (OpenAI-compatible endpoint):

```toml
[provider]
kind = "http"
base_url = "http://localhost:11434/v1"
model = "llama3.2"
timeout = 60
context_limit = 8192
```

OpenRouter:

```toml
[provider]
kind = "http"  # "openrouter" is accepted as an alias for the same generic endpoint
base_url = "https://openrouter.ai/api/v1"
model = "openai/gpt-4o-mini"
api_key_env = "OPENROUTER_API_KEY"
timeout = 60
context_limit = 8192
```

A minimal offline run uses the deterministic fake provider and requires a clean, committed Git repository.

Unknown natural-language criteria are rejected by the final verifier rather than treated as
successful. Use a deterministic criterion or extend the verifier explicitly.
