# Troubleshooting

Start with the trace; it usually says exactly what happened:

```bash
gcae inspect <repo> <run-id>                     # status, criteria, recovery, degradations
ls  "${XDG_STATE_HOME:-$HOME/.local/state}/gcae/runs/<run-id>/"
tail -50 "${XDG_STATE_HOME:-$HOME/.local/state}/gcae/runs/<run-id>/events.jsonl"
```

## The run stops by itself

Runs stop for reasons that are all recorded, and accepted work is always kept:

| Status | Meaning | Next step |
| --- | --- | --- |
| `complete` | every criterion verified | nothing; the branch is merged |
| `failed: provider output …` | the model produced unusable output even after retries and a diagnosis | check the model/config, then `gcae resume` |
| `failed: step budget exhausted …` | `max_steps` reached with the plan unfinished | raise `[runtime] max_steps`, or `gcae resume` — it gets `recovery_budget` extra iterations after a successful diagnosis |
| `failed: run state could not be written …` | the state file is not writable (disk full, permissions) | fix the filesystem; the accepted commits are still on the branch |
| `failed: unexpected …` | a bug — the exception is recorded as a lesson | `gcae inspect` shows the reason; the run directory and commits are intact |
| `waiting_for_user` | the run diagnosed itself and needs your decision | read the question, answer with `gcae resume` (dashboard: press `i`) |
| `stopped` | you stopped it | `gcae resume` |

## "Nothing happened" after the run

The work is on the run branch, not in your checkout, when the merge did not happen:

```bash
git -C <repo> branch --list 'gcae/*'
gcae merge <repo> <run-id>      # brings the accepted commits into your tree
```

The usual causes are a dirty checkout, a branch that moved, or `auto_merge = false`. The reason is
printed at the end of the run and recorded in `state.json`.

## GCAE refuses to start

| Message | Cause |
| --- | --- |
| `another GCAE run is already working on <repo> (run …)` | the repository lock; wait for the other run, or check `gcae list` |
| `repository has an unresolved merge` | a mid-merge/rebased repository is never modified — finish it first |
| `not a git repository` | point GCAE at a repository, or let it bootstrap one |
| `no config file found … the built-in default provider is the fake one` | pass `--config`, or create `./config.toml` |

## A model call stalls

`provider request stalled: no data for 45s` means the endpoint accepted the request and produced
nothing. Lower `provider.stall_timeout` to notice sooner, check the provider status, and remember
that a cancelled call is a normal failure the ladder handles. Slowness is visible in the dashboard
(first-token latency, characters received, a heartbeat every 10 s) — a run that looks frozen for more
than a heartbeat interval is a bug worth reporting.

## The run says `degraded:`

Memory or the event log failed and the run continued without that subsystem (a locked database, a
full disk). The work still has to pass every gate; only the record is incomplete. Fix the cause, then
finish or resume the run. A **state** failure is different: it stops the run rather than continuing
with a resume point that could be wrong.

## The run escalated or failed over

`model_escalated` / `model_failover` mean the model in use changed: the controller moved to
`[models.escalation]` because the same failure repeated, or because that role's model stopped
answering. Later decisions use the new model; the timeline and `gcae inspect` say which one.

## The dashboard shows `measuring…` or empty panels

Some panels read Git state off the UI thread and show `measuring…` until the first read completes. If
a panel stays empty while the log advances, press `l` — the raw event log is the ground truth — and
report it with the run id.

## Still stuck?

- `gcae list` shows whether the run is really still going.
- `events.jsonl` ends with the last thing that happened.
- `gcae undo` reverses a merge you did not want; the branch is never deleted by a failure.
