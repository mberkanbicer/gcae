# FAQ

**Is this a wrapper around an LLM that runs shell commands?**
Partly. The difference is what happens around each action: deterministic validation, a structured
evaluation decision, a Git checkpoint or rollback, and a final verification gate before anything
reaches your branch.

**What makes it "Git-checkpointed"?**
Every accepted semantic step is a commit on the run branch, and `accepted_commit` is always a tree
that passed validation, evaluation and verification. Rejection is `reset --hard` inside the run's own
worktree, so the failure costs nothing but the tokens spent.

**Why does it not just use `git stash` in my working tree?**
Because your checkout is not the workshop. One run owns one worktree; the only thing GCAE ever does to
your checkout is the recorded, reversible merge.

**Does it need a specific model?**
No. Anything OpenAI-chat-compatible works, including a local Ollama model. Quality varies: the loop's
guards (repetition, step budget, validation, verification) keep a weak model bounded, and
`[models.escalation]` lets a stronger model take over when it struggles.

**Can it run several tasks at once?**
Not in the same repository. One run, one worktree, no cross-run lock: two concurrent runs on one
repository are unsupported.

**Where does my data go?**
Nowhere except your model provider. Memory, events, diffs and tool results stay in
`~/.local/state/gcae`. Nothing is uploaded; there is no telemetry.

**Can I use it in CI?**
Yes — `--headless` prints the state as JSON on stdout, the log on stderr, and exits non-zero unless
the run completed *and* the work reached the checkout. `[runtime] auto_merge = false` keeps CI
branches separate.

**How do I trust the result?**
By reading the same evidence GCAE read: `gcae inspect` for criteria and verification,
`runs/<run-id>/diffs/` for what each step changed, and `git log` on your branch for the commits that
were accepted.

**Does it ever modify files outside the worktree?**
No. The command tool enforces workspace confinement, and the runtime's Git operations target the run
worktree or the recorded merge only.

**Is the model allowed to run `git`?**
No. `git reset|clean|commit|worktree|…` are blocked in the command tool; all Git work is owned by the
runtime. That is what keeps checkpoints trustworthy.
