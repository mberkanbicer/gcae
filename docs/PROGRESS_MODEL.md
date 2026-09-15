# Progress model

Three presentation levels, never mixed:

- **Level 1 — main progress** renders `ProgressEvent` (`src/gcae/progress.py`): one line
  per meaningful engineering action with its outcome. Categories are a small fixed set —
  plan, inspect, edit, execute, test, validate, observe, failure, diagnose, recover,
  rollback, replan, accept, verify, user, system.
- **Level 2 — details** expands one progress event: command, files, output excerpts,
  evaluator reason, evidence records, recovery strategy. Timeline rows carry the payload;
  `[t]` shows trajectory attempts, `[h]` guardian health.
- **Level 3 — raw logs** keeps every runtime event including model streaming telemetry.
  The Logs screen defaults to the semantic filter; MODEL/DEBUG/ALL are one key away.

## Rules

- Model streaming telemetry (characters, reasoning counts, chunks, heartbeats) never
  becomes progress. The main screen shows at most `controller · generating · 3.4s`.
- Consecutive file reads group into one `INSPECT` line ("Reviewed 4 relevant files");
  outcomes resolve their line in place (`▶ run tests · FAILED · …`) instead of adding one.
- Routine `continue` votes and supporting evidence are not progress lines; failures,
  recoveries, contradictions and user events always are.
- Progress events are presentation data derived from runtime events. They duplicate no
  authoritative state; the row wording lives in `formatters.timeline_entry`.

## Active panel contract

Every moment answers five questions: **Goal** (current semantic goal), **Doing**
(current action with phase), **Last result** (latest meaningful outcome), **Next**
(the explicit operational next step — never chain-of-thought), **Health** (guardian
state, compact; detail on `[h]`). The top bar shows exactly one primary state with
total priority: fatal → terminal → blocked → waiting → recovering → phase.
