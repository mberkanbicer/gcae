# Test plan

Tests cover Pydantic/state persistence, bounded provider repair, Git isolation and rollback, generated-artifact cleanup, immutable FTS memory, punctuation-safe retrieval, pinned context budgeting, tool boundaries and patch traversal, deterministic validation, safeguards, HTTP headers/generation forwarding, config loading, resume contracts, fail-closed final verification, and a mandatory end-to-end bad-then-good rollback trajectory.

Run:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/mypy src/gcae
```
