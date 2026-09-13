You are implementing a new Python project from scratch named **GCAE — Git-Checkpointed Adaptive Execution**.

Your goal is to build a lightweight, single-agent runtime for coding and project tasks. The system must favor correctness, reversibility, minimal scope, repository cleanliness, explicit state, and low context usage.

Do not substitute this architecture with an existing agent framework.

## Primary objective

Implement a production-quality but deliberately small agent runtime with the following behavior:

1. Analyze a user's task.
2. Extract objective, hard constraints, success criteria, assumptions, and an initial plan.
3. Create one isolated Git worktree for the run.
4. Work in small semantic steps.
5. Treat all uncommitted work as speculative.
6. After each semantic step:

   * inspect the resulting Git delta,
   * run deterministic validation,
   * evaluate whether the change advances the objective,
   * accept, rollback, or replan.
7. An accepted step becomes a Git checkpoint commit.
8. A rejected step returns execution to the last accepted commit.
9. Knowledge learned from rejected paths must remain in persistent memory.
10. Reconstruct LLM context on demand instead of accumulating conversation history.
11. Verify every success criterion before declaring completion.
12. Leave the target project clean and free of unnecessary artifacts.

## Non-goals

Do NOT introduce:

* LangGraph
* LangChain
* CrewAI
* AutoGen
* multi-agent architecture
* vector databases
* embeddings
* recursive conversation summarization
* knowledge graphs
* background daemons
* workflow engines
* message buses
* ORM frameworks
* plugin frameworks
* separate worktree per step
* automatic merging into the user's main branch
* provider-specific orchestration logic unless strictly required

Do not add abstractions before a demonstrated need exists.

## Technology constraints

Use:

* Python >= 3.12
* Pydantic v2
* httpx
* SQLite from the standard library
* SQLite FTS5 for memory retrieval
* TOML configuration using tomllib
* subprocess for Git
* argparse for CLI
* logging from the standard library
* pytest
* ruff
* mypy

Keep runtime dependencies minimal.

Prefer standard-library functionality whenever it is sufficient.

## Provider architecture

Implement one generic OpenAI-compatible provider.

It must support configurable:

* base_url
* model
* API key or API-key environment variable
* timeout
* context limit
* generation parameters

It should be usable for both OpenRouter and OpenAI-compatible local endpoints such as Ollama.

Do not depend on native model tool-calling.

Models return structured JSON decisions. The runtime validates those decisions with Pydantic and executes tools itself.

Invalid structured output may receive a small bounded repair retry. Never enter unbounded repair loops.

## Core architectural invariant

Separate:

### Execution state

Execution state is reversible.

Git represents trusted execution state.

An accepted Git commit is a trusted checkpoint.

Uncommitted changes are speculative.

On rejection:

* reset --hard to accepted_commit
* clean candidate untracked files inside the isolated agent worktree

Never perform destructive reset or clean operations in the user's original working directory.

### Knowledge state

Knowledge state is cumulative.

Rollback must not remove:

* discovered facts
* accepted decisions
* failure lessons
* relevant observations
* artifact references

This distinction is fundamental and must be explicit in the code.

## Git execution model

A run uses exactly one isolated worktree.

The user's original repository must remain untouched.

Require a committed base revision for V1.

If the source repository contains uncommitted user changes, fail safely with a clear message rather than manipulating them.

Create a dedicated run branch such as:

gcae/<run-id>

Create the worktree outside the source project, under the configured runtime directory.

Before each semantic step ensure the worktree matches accepted_commit and has no speculative leftovers.

After evaluator acceptance:

* stage intended changes
* create a semantic checkpoint commit
* update accepted_commit

After rollback:

* reset to accepted_commit
* clean candidate files
* verify that the worktree is clean

Never automatically merge the final branch into the user's branch.

## Runtime storage

By default store GCAE runtime state outside target repositories.

Use an XDG-compatible state location, for example:

${XDG_STATE_HOME:-~/.local/state}/gcae/

Per run retain:

* state.json
* events.jsonl
* tool results
* relevant diffs
* run metadata

Use SQLite for structured memory.

## Persistent memory

Support at least these memory types:

* fact
* decision
* failure
* observation
* artifact
* user_instruction

Memory records should carry provenance where applicable:

* run_id
* step_id
* source
* commit SHA
* created_at
* importance
* immutable flag

Original user request, explicit user constraints, success criteria, and later user overrides must be stored losslessly.

They must never be recursively summarized.

## Context management

The LLM context is not memory.

Build fresh context for every model call.

Context should be reconstructed from:

* original objective
* hard constraints
* success criteria
* current plan
* current semantic goal
* accepted commit
* relevant facts
* relevant decisions
* relevant failed paths
* active files
* latest observations
* Git delta
* validation results

Implement token-budget sections.

Pinned information must survive budget pressure:

* original request
* hard constraints
* success criteria
* current goal
* latest explicit user instruction
* accepted commit
* critical failure lessons

Low-priority data should be removed before pinned information.

Do not recursively summarize summaries.

If summaries are used as optional caches, retain source record IDs so the original information can be retrieved.

Use SQLite FTS5 for V1 retrieval.

Do not implement embeddings.

## Semantic-step model

The execution unit is a semantic step, not an individual tool call.

A semantic step should describe:

* goal
* rationale
* expected result
* intended scope
* validation requirements

Several reads, searches, patches, or test commands may belong to one semantic step.

Do not perform Git checkpoint evaluation after every read operation.

## Decision contract

The controller must return a validated structured decision containing an action from:

* execute_tool
* continue
* replan
* finish
* ask_user

Include:

* semantic_goal
* reason_summary
* optional tool name
* optional tool arguments
* expected result

Only tools registered in the runtime may execute.

## Tool system

Implement a small explicit registry.

Initial tools:

* list files
* read file
* search text
* apply patch
* create a necessary new file
* run a command

Git checkpoint operations are runtime-owned and must not be exposed as unrestricted model tools.

All filesystem writes must resolve inside the active agent worktree unless writing to the dedicated GCAE runtime directory.

Reject path traversal and workspace escape.

Command execution must:

* use the agent worktree as cwd
* reject obviously destructive system-level commands
* reject sudo/system shutdown/filesystem formatting operations
* have configurable timeout
* capture stdout/stderr/exit code
* avoid modifying the user's system environment

Package installation and external network actions should not occur implicitly.

## Deterministic validation

Before semantic LLM evaluation, gather inexpensive deterministic evidence where applicable:

* command exit codes
* syntax or compile status
* configured targeted tests
* git diff --check
* changed file list
* new file list
* deleted file list
* dependency-manifest changes
* unexpected scope changes
* configured project quality commands

These results must be passed to the evaluator.

## Workspace hygiene

The agent must not consider a task successful merely because tests pass.

Every accepted change must be:

* correct
* necessary
* clean
* maintainable

Prefer:

* modifying existing structures over unnecessary new files
* reusing existing abstractions over parallel implementations
* root-cause fixes over patches
* project conventions over introducing new conventions
* deleting obsolete code over retaining duplicate paths

Do not leave:

* temporary scripts
* debug logs
* commented-out alternatives
* backup files
* unused imports
* dead code
* speculative configurations
* duplicate implementations
* unused dependencies
* unrelated refactors

New dependencies, new files, new abstractions, and broad refactors require explicit justification.

Minimality means the smallest sufficient architectural change, not the smallest line count.

Never sacrifice correctness or maintainability merely to reduce the diff.

## Evaluator

The evaluator must produce one of:

* accept
* rollback
* replan
* continue
* finish_candidate

Its input should include only relevant reconstructed state, especially:

* objective
* current semantic goal
* hard constraints
* relevant memory
* current Git diff
* deterministic validation
* latest observations
* relevant failed paths

It should assess:

* measurable progress toward the objective
* correctness
* requirement compliance
* scope discipline
* unnecessary architecture
* repository hygiene
* regressions
* whether assumptions were invalidated

Rejected paths must be persisted as concise negative-trajectory memory so they are not repeated.

## Replanning

Do not replan after every step.

Replan only when justified, such as:

* invalid assumption
* blocked route
* repeated failure
* new user constraint
* contradictory evidence
* materially better route
* changed understanding of the target project

Preserve completed valid work whenever possible.

## Loop and stagnation protection

Implement bounded safeguards.

Detect:

* repeated identical tool call and arguments
* repeated identical error
* repeated failure of the same semantic strategy
* multiple iterations without accepted progress
* repeated replanning without new evidence

When stagnation is detected, force strategy reconsideration rather than repeating the same action.

Never implement an unbounded loop.

## Final verification

A controller request to finish is only a finish candidate.

A separate final verifier must check every success criterion.

Use deterministic evidence wherever possible.

Examples:

* test passes
* file exists
* command succeeds
* public signature unchanged
* expected artifact exists
* no unexpected files remain

Use semantic LLM judgment only when the criterion cannot be deterministically established.

Completion requires all mandatory success criteria to pass.

After verification perform a final hygiene check.

## Resume behavior

Persist sufficient state to resume safely.

On resume:

1. load persisted state,
2. verify repository/worktree identity,
3. restore the last accepted checkpoint,
4. remove stale speculative changes only inside the isolated agent worktree,
5. rebuild context from persistent state,
6. continue execution.

Do not rely on previous LLM conversation history to resume.

## Required data models

Implement typed Pydantic models for at least:

* AgentState
* PlanStep
* SemanticStep
* Decision
* ToolCall
* ToolResult
* ValidationResult
* Evaluation
* MemoryRecord
* MemoryCandidate
* VerificationReport
* CriterionResult
* Event

Avoid generic untyped dictionaries where a stable contract exists.

## State machine

Keep orchestration explicit in Python.

Do not introduce a workflow framework.

The high-level runtime loop should remain readable in one place.

A developer should be able to inspect runtime.py and understand the entire lifecycle without following dozens of abstraction layers.

## Required documentation

Before implementation is considered complete, provide:

docs/ARCHITECTURE.md
docs/STATE_MACHINE.md
docs/GIT_EXECUTION.md
docs/MEMORY_CONTEXT.md
docs/WORKSPACE_HYGIENE.md
docs/PROVIDERS.md
docs/TOOLS.md
docs/TEST_PLAN.md

Documentation must describe the implemented system, not hypothetical future functionality.

## Implementation phases

Implement in this order.

Phase 0:
Project scaffold, pyproject, docs skeleton, package skeleton, lint/type/test setup.

Phase 1:
Pydantic contracts, state persistence, explicit state machine, fake deterministic provider.

Phase 2:
Git repository abstraction, isolated worktree creation, accepted checkpoint handling, commit, rollback, cleanup.

Phase 3:
SQLite memory, FTS5 retrieval, immutable records, JSONL event log, context reconstruction and budgeting.

Phase 4:
Tool registry, filesystem boundaries, search, patch application, command execution, deterministic validation.

Phase 5:
Planner, controller, evaluator, verifier, replanning, runtime orchestration.

Phase 6:
Workspace hygiene checks, repeated-action detection, stagnation detection, failure trajectory memory.

Phase 7:
Generic OpenAI-compatible provider usable by local endpoints and OpenRouter.

Phase 8:
CLI, configuration, resume support, user-facing run summary.

Phase 9:
End-to-end hardening and documentation finalization.

Do not jump ahead and build optional features before the current phase is tested.

After every phase run:

pytest
ruff check .
mypy src/gcae

Do not proceed with knowingly failing checks.

## Mandatory tests

Include strong unit and integration coverage for the architectural invariants.

At minimum verify:

* state round-trip persistence
* immutable memory preservation
* context pinned records survive budget reduction
* FTS retrieval works
* provider structured-output validation
* invalid provider output receives bounded repair handling
* target source repository remains unchanged
* worktree is isolated
* accepted commit changes only after acceptance
* rollback restores accepted commit
* rollback removes candidate untracked files
* rollback preserves failure memory
* workspace escape is rejected
* dangerous command attempts are rejected
* repeated-action detection works
* stagnation detection works
* resume restores the last trusted state
* final verification prevents premature completion

Create a full integration test using a temporary Git repository and a fake model trajectory:

1. The fake model chooses an incorrect implementation.
2. Validation/evaluation rejects it.
3. The runtime rolls back.
4. The failure is recorded in memory.
5. The fake model chooses a different correct implementation.
6. Validation/evaluation accepts it.
7. A checkpoint commit is created.
8. Final verification passes.

At the end assert:

* the user's original working tree was untouched,
* the rejected implementation is gone,
* the failure lesson still exists,
* the correct implementation exists,
* the accepted commit points to the correct state,
* no temporary project artifacts remain.

This integration test is mandatory.

## Scope discipline

While implementing GCAE itself, apply the same discipline GCAE is intended to enforce.

Do not:

* create duplicate managers/services/controllers,
* add compatibility layers without a concrete need,
* keep two implementations of the same subsystem,
* introduce speculative extension points,
* add unused configuration options,
* add packages merely for convenience,
* create generic abstractions used once,
* refactor unrelated code.

If a simpler implementation satisfies the explicit requirements with equal correctness and maintainability, use the simpler implementation.

## Completion criteria

The project is complete only when:

* all mandatory architecture is implemented,
* tests pass,
* ruff passes,
* mypy passes,
* end-to-end rollback trajectory test passes,
* source repositories remain protected,
* context can be rebuilt without conversation history,
* memory survives rollback,
* resume works,
* final verifier prevents false completion,
* runtime does not leave temporary artifacts in target projects,
* documentation matches actual behavior,
* no mandatory behavior depends on an external agent framework.

At the end, provide:

1. final repository tree,
2. concise architecture summary,
3. exact commands to install and run,
4. exact commands to run tests/lint/type checks,
5. a sample local Ollama configuration,
6. a sample OpenRouter configuration,
7. one minimal example run,
8. known limitations that genuinely remain.

Do not claim functionality that is not implemented or tested.
