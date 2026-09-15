# Execution state and knowledge state

The central invariant: **execution may move backward, knowledge must not.**

| | Execution state | Knowledge state |
| --- | --- | --- |
| File | `runs/<id>/state.json` + the git branch | `memory.db` |
| Semantics | reversible | cumulative, append-only |
| Contents | accepted commit, candidate, plan, current step, validation, trajectory | user instructions, facts, decisions, failures, observations, evidence |
| On rejection | `reset --hard accepted_commit` + clean worktree | lesson and evidence records stay |
| On acceptance | checkpoint → new trusted commit | accepted decision recorded |

`MemoryStore.update` refuses immutable records; rollback never touches the store; the
evidence ledger lives in the same database and is equally cumulative. The invariant is
tested directly (`tests/test_hardening.py`): a rejected trajectory leaves the worktree at
the trusted commit while its failure lesson and its evidence remain retrievable.

Git is the mechanism for reversible execution state (V1). It is *not* the definition of
knowledge state — that is what `memory.db` is for.
