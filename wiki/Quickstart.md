# Quickstart

## 1. Configure a provider

Create `config.toml` in the directory you run GCAE from (or in `~/.config/gcae/config.toml`, or point
`GCAE_CONFIG` at it — see [Configuration](Configuration)). GCAE prints the file it used:

```toml
[provider]
kind = "openrouter"
base_url = "https://openrouter.ai/api/v1"
api_key_env = "OPENROUTER_API_KEY"
model = "qwen/qwen3-coder"

[provider.generation]
temperature = 0.0
max_tokens = 2048
```

A local server needs no key:

```toml
[provider]
kind = "http"
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:14b"
```

If no config file exists at all, GCAE says so — the built-in default provider is the *fake* one, and
`run` warns instead of failing later with a confusing model error.

## 2. Prepare the target repository

Git repository, a committed base, no merge in progress. If the repository is unborn or dirty, GCAE
creates the base commit it needs (respecting `.gitignore`, bounded to 2000 files / 50 MB) and reports
it as a notice; `--no-auto-bootstrap` refuses instead. A repository mid-merge is refused.

## 3. Run a task

```bash
# interactive dashboard: type the task, the planner derives the criteria
gcae run /path/to/repo

# fully specified, non-interactive (reproducible criteria)
gcae run /path/to/repo "Serve /health on port 8000" \
  --criterion "file contains: app.py :: /health" \
  --criterion "command succeeds: python -c 'import app'" \
  --headless
```

## 4. Read the result

The summary prints the run id, the accepted commit, what changed and whether the merge happened:

```
gcae: merged gcae/2f4ac1b0c3e9 into main (b21f0aa1 -> 5c01d9ab)
```

```bash
gcae inspect <repo> <run-id>          # plan, criteria, verification, recovery, degradations
gcae inspect <repo> <run-id> --json   # the persisted state, verbatim
git -C /path/to/repo log --oneline -3
```

## 5. Undo, inspect, resume

```bash
gcae list /path/to/repo               # known runs, newest first
gcae undo /path/to/repo <run-id>      # reverse the merge (the branch stays)
gcae resume /path/to/repo <run-id>    # continue a run that stopped or is waiting for you
```

## If a run stops

| The run is… | What to do |
| --- | --- |
| `waiting_for_user` | it diagnosed its own trace and needs a decision; read the question, then `gcae resume` or answer in the dashboard with `i` |
| `failed: … ` | the reason names the cause; accepted commits are on the branch — `gcae merge` brings them in |
| `stopped` | you stopped it; `gcae resume` continues from the accepted checkpoint |
| refused at start | another run holds the repository lock, or the repository is mid-merge — see [Troubleshooting](Troubleshooting) |

Each of those states keeps the accepted work; nothing is thrown away by a failure.
