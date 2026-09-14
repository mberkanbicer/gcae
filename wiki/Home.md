# GCAE — Git-Checkpointed Adaptive Execution

GCAE runs one coding task at a time against a Git repository. It plans the task as **semantic
steps**, works inside a dedicated Git worktree, validates each step deterministically, asks a model
whether the step actually advanced the objective, and then either **checkpoints** the step as a
commit or **rolls it back** and replans. A final verification gate checks every success criterion
before the run is declared complete. The verified branch is merged into your checkout, and that
merge is reversible.

**Trusted state is committed state.** Nothing reaches your branch unless validation, evaluation and
final verification accepted it.

## What you get

| Guarantee | How it is kept |
| --- | --- |
| Verified work only | `accepted_commit` is always a commit that passed validation, evaluation and final verification. |
| Failure is cheap | A rejection is `reset --hard` plus cleanup **inside the worktree only**; your checkout is untouched. |
| Rollback is not amnesia | Facts, decisions and failure lessons live in SQLite and survive every rollback. |
| Self-recovery is the default | Stagnation, unusable model output, a stall, an exhausted budget, a planner outage, a transient network error, a dead model and even an unexpected exception are diagnosed from the run's own trace before anything stops. |
| A slow model never looks frozen | Model calls stream; the dashboard shows elapsed time, first-token latency, character counts, and a heartbeat every 10s. Silence past `stall_timeout` fails honestly. |
| One run per repository | `run`, `resume`, `merge` and `undo` hold a lock, so two runs cannot interleave two merges into one branch. |
| The loop owns Git | Base commit, branch, worktree, checkpoints, merges, conflict resolution and cleanup are automatic. The model is never allowed to run `git`. |
| It explains itself | Every failure has a reason in the log, the CLI summary and the dashboard; lost subsystems are reported instead of hidden. |

## Where to go next

| If you want to… | Read |
| --- | --- |
| install it and run something | [Installation](Installation), [Quickstart](Quickstart) |
| understand the vocabulary | [Concepts](Concepts) |
| know how it is built and how it recovers | [Architecture](Architecture) |
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
- Not a goal-ignoring optimiser: it stops, asks or fails rather than merging work it cannot verify.

## Project

- Source: <https://github.com/mberkanbicer/gcae>
- Releases (wheel + sdist): <https://github.com/mberkanbicer/gcae/releases>
- License: MIT
- Documentation: [`docs/`](https://github.com/mberkanbicer/gcae/tree/main/docs) in the repository
- Changelog: [`CHANGELOG.md`](https://github.com/mberkanbicer/gcae/blob/main/CHANGELOG.md)
