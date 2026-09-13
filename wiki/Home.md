# GCAE — Git-Checkpointed Adaptive Execution

GCAE runs one coding task at a time against a Git repository. It plans the task as **semantic
steps**, works inside a dedicated Git worktree, validates each step deterministically, asks a model
whether the step actually advanced the objective, and then either **checkpoints** the step as a
commit or **rolls it back** and replans. A final verification gate checks every success criterion
before the run is declared complete. The verified branch is merged into your checkout, and that
merge is reversible.

**Trusted state is committed state.** Nothing reaches your branch unless validation, evaluation and
final verification accepted it.

## Where to go next

| If you want to… | Read |
| --- | --- |
| install it and run something | [Installation](Installation), [Quickstart](Quickstart) |
| understand the vocabulary | [Concepts](Concepts) |
| know how it is built | [Architecture](Architecture) |
| use every command | [CLI Reference](CLI-Reference) |
| work in the dashboard | [Dashboard](Dashboard) |
| point it at a model | [Configuration](Configuration), [Providers](Providers) |
| know what it remembers and what the model sees | [Memory and Context](Memory-and-Context) |
| understand branches, checkpoints and merges | [Git Model](Git-Model) |
| fix a problem | [Troubleshooting](Troubleshooting), [FAQ](FAQ) |

## What it is not

- Not a sandbox: `run_command` is a blocklist plus workspace confinement, not OS isolation.
- Not a multi-agent framework: one agent, one run, no orchestration graph.
- Not a hosted service: everything runs locally; the only network calls go to your model provider.
- Not a substitute for your judgement: when a task is ambiguous the run asks instead of guessing.

## Project

- Source: <https://github.com/mberkanbicer/gcae>
- License: MIT
- Documentation: [`docs/`](https://github.com/mberkanbicer/gcae/tree/main/docs) in the repository
- Changelog: [`CHANGELOG.md`](https://github.com/mberkanbicer/gcae/blob/main/CHANGELOG.md)
