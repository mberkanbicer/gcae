# Test plan

Tests cover Pydantic/state persistence, bounded provider repair, Git isolation and rollback,
generated-artifact cleanup, immutable FTS memory, FTS refresh after update, cross-run memory
sharing, punctuation-safe retrieval, pinned context budgeting, tool boundaries and patch
traversal, deterministic validation, deterministic and LLM evaluators, evaluator memory
promotion, runtime lifecycle logging, safeguards, controller prompt contract, HTTP
headers/generation forwarding, config loading, provider and evaluator kind resolution, resume
contracts, fail-closed final verification, premature-completion blocking, final checkpointing of
verified speculative changes, merge confirmation and reversible merge undo, and a mandatory
end-to-end bad-then-good rollback trajectory.

The integration test asserts that the source worktree is untouched, the rejected implementation is
gone, the accepted commit never changed while the rejected step was evaluated, the failure lesson
survives rollback, the accepted commit equals the verified worktree, and tool-result/diff artifacts
exist in the run directory.

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy src/gcae
```
