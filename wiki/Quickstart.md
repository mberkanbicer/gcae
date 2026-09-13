# Quickstart

## 1. Configure a provider

```bash
cp config.example.toml config.toml      # config.toml is gitignored: it holds your API key
```

Local model (no key):

```toml
[provider]
kind = "http"
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:14b"

[validation]
commands = ["pytest -q"]
```

Hosted model:

```toml
[provider]
kind = "openrouter"
base_url = "https://openrouter.ai/api/v1"
model = "your/model"
api_key_env = "OPENROUTER_API_KEY"      # or api_key = "..." (never commit this)
```

## 2. Prepare the target repository

Any Git repository works. It does not need to be pristine: GCAE creates the base commit a run needs,
commits pending edits as that base (without discarding them), and uses `GCAE <gcae@localhost>` when
no Git identity is configured. A repository in the middle of a merge, rebase or cherry-pick is
refused, because that state is yours to finish.

## 3. Run a task

```bash
# interactive dashboard: it asks for the task and the planner derives the criteria
gcae run ~/src/project --config config.toml

# fully specified, non-interactive
gcae run ~/src/project "add a --dry-run flag to the importer" \
  --criterion "command succeeds: pytest -q" \
  --config config.toml
```

Criteria GCAE understands deterministically:

```
file exists: path/to/file
file contains: path/to/file :: text
file contains exactly: path/to/file :: text
command succeeds: pytest -q
```

Add `--criterion` for anything the planner might miss; user criteria are merged with inferred ones,
never overwritten.

## 4. Read the result

```console
run d1ae6b3af14d: complete
accepted steps: 1, commit: d176e5f2effb87af4108bafb4ea0b5d699fecc59
verification: 1/1 criteria passed
worktree: ~/.local/state/gcae/worktrees/d1ae6b3af14d
branch: gcae/d1ae6b3af14d merged into main (undo: gcae undo ~/csv-parser d1ae6b3af14d)
files: parser.py
documents: ~/csv-parser (in your working tree now)
```

The last two lines are the ones to read: `files:` lists what the run produced, `documents:` names the
folder that currently holds it.

## 5. Undo, inspect, resume

```bash
gcae list                                   # runs, status, accepted steps, merge state
gcae inspect d1ae6b3af14d                   # objective, plan, verification, merge record
gcae undo ~/src/project d1ae6b3af14d        # reverse the merge
gcae resume ~/src/project d1ae6b3af14d      # continue a stopped, failed or waiting run
```

## If a run stops

| Status | Meaning | What to do |
| --- | --- | --- |
| `complete` | verified, merged | nothing |
| `waiting_for_user` | the agent needs a decision | answer with `gcae resume` or `i` in the dashboard |
| `failed: …` | it gave up after bounded attempts | read the reason, then `gcae resume` or start a new run |
| `stopped` | you stopped it | `gcae resume` |

Accepted checkpoints survive all four cases, and they are merged into your checkout even when the
final verification did not pass — labelled as unverified.
