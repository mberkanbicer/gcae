# FAQ

**Does GCAE modify my working tree?**
Not while it works. It edits a worktree under the state directory, one per run. Your checkout changes
only when a verified run merges, and `gcae undo` reverses that merge.

**Do I need a GPU or a local model?**
No. Any OpenAI-compatible endpoint works. A local Ollama/LM Studio server is fine for small tasks; a
stronger model produces better plans and corrections.

**How do I know the work is actually correct?**
Deterministic criteria decide completion: `file exists:`, `file contains:`, `file contains exactly:`,
`command succeeds:`. A model judge is opt-in (`verifier kind = "hybrid"`) and fails closed. GCAE never
declares success on the model's word alone.

**What happens when the model is wrong or the endpoint dies?**
The run recovers in this order: retry transient errors with backoff → fail over to
`[models.escalation]` → escalate on repeated failures → diagnose its own trace → ask you → fail.
Bounded at every step, and accepted commits are never lost.

**Can I run it unattended?**
Yes: `gcae run <repo> "task" --criterion … --headless --merge`. Exit code `0` means the run finished
and verified; anything else means read `gcae inspect`.

**Can two runs work on the same repository?**
No — that would interleave two merges into one branch. The second run is refused with the id of the
run holding the lock. Runs on different repositories are independent.

**What if a run needs to ask me something?**
It stops as `waiting_for_user`, prints the question, and exits non-zero. Answer with
`gcae resume`, or press `i` in the dashboard and type your instruction.

**Where do my prompt and code go?**
To the provider you configured, and nowhere else. Prompts are reconstructed per call and are not kept
as a growing conversation. Memory stays in a local SQLite file.

**How much does a run cost?**
Each iteration is one controller call (plus planner, evaluator, verifier and possible recovery calls).
`max_steps` bounds iterations, `recovery_budget` bounds self-corrections, so a run has a known ceiling.
Fast models for controller and planner, a strong one for recovery, is the configuration that has
worked best in practice.

**Can I edit files while a run is going?**
Its worktree is separate, so yes — but the merge may then conflict. A conflicting merge is handed to
the agent, re-verified and retried, or left on the branch with an explanation.

**Is there an undo for everything?**
`gcae undo` reverses a recorded merge. Rejected steps never needed undoing (they were rolled back
inside the worktree), and a stopped run keeps its branch, so nothing is lost by failure.

**Why does it say the planner is `deterministic`?**
`[planner] kind = "auto"` uses the deterministic planner when your criteria are already explicit and a
model planner otherwise. The deterministic planner is also the fallback when a planner call fails.

**Does it work without a TUI?**
Yes. The engine never imports Textual; `--headless` is a first-class mode and the only difference is
where the events are rendered.
