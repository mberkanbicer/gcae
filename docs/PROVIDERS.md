# Providers

One generic OpenAI-compatible provider serves OpenRouter, Ollama, vLLM, LM Studio and similar
endpoints. `provider.kind = "http"` or `"openrouter"` selects it; `"fake"` selects the deterministic
offline provider. Unknown kinds are rejected — never silently replaced.

Configurable per provider: `base_url`, `model`, `api_key` or `api_key_env`, `timeout`,
`context_limit` and `generation` parameters. The HTTP provider posts to `/chat/completions` with
`response_format: json_object`, forwards the generation mapping and sends `Authorization: Bearer`
when a key is configured. Native tool calling is not used: the model returns structured JSON that
the runtime validates and executes.

## Streaming, liveness and stalls

With `[provider] stream = true` (default) completions are requested as a server-sent event stream,
and the runtime turns each update into events so a slow model is visible instead of looking frozen:

| Event | Meaning |
| --- | --- |
| `provider_started` | a role's call began (role + model) |
| `provider_first_token` | first content arrived, with the connection latency |
| `provider_progress` | incremental size (content and reasoning characters) plus a preview tail, rate limited |
| `provider_waiting` | heartbeat: the call is running and produced nothing for 10s |
| `provider_finished` | wall-clock time and the final character counts |

Nothing arrives for `[provider] stall_timeout` seconds (default 45, applied per read) → the call fails
with `provider request stalled: no data for Ns …` and the run hands that to the recovery ladder
instead of waiting forever. Measured against a socket that accepts and never answers: 3s to detection
with `stall_timeout = 3`.

### Retries

Transient failures — 429, 5xx, timeouts and dropped connections — are retried inside the provider
with exponential backoff, jitter and `Retry-After` support (`[provider] retries`, `retry_backoff`).
Client errors (400, 404, 422) are not retried: they are configuration or request problems, and the
run's recovery ladder is a better answer than repeating them. Streaming calls retry as streams, so a
slow answer is never silently downgraded to a buffered one.

### Failover

A provider outage is the one failure the recovery advisor cannot reason its way out of — the advisor
would have to call the same broken endpoint. When `[models.escalation]` is configured it becomes the
fallback: if the controller, planner, evaluator or verifier model fails (an invalid model id, a 4xx,
a dead endpoint), that role is switched to the fallback once and the run continues. The switch is
recorded as a `model_failover` event with the failing role and the new model.

Verified live with a deliberately invalid controller model: `model_failover` → run completed in 11s
instead of failing.

The evaluator is a special case worth knowing: after switching, the *same* work is evaluated again on
the fallback, so an accepted step is not thrown away because a judge was offline. And when the same
rejection repeats three times, the controller escalates to the fallback model before the run asks the
user — the approach is exhausted, not just the attempt.

Streaming is fail-soft. An endpoint that answers a streaming request with a buffered body, or refuses
it outright (HTTP 4xx), is retried automatically without streaming; a *timeout* is not retried that
way, because a silent endpoint would simply be silent again.

## Reasoning models and structured output

Reasoning models (OpenRouter returns `message.reasoning` separately) can spend their whole output
budget deliberating. GCAE handles the two failure modes it has observed:

| Symptom | Cause | GCAE behaviour |
| --- | --- | --- |
| `content` is empty, `finish_reason=length`, huge `reasoning` | `response_format=json_object` makes the model deliberate to the token limit | retries **without** `response_format` (the prompt already demands JSON and the schema is validated) |
| valid JSON start, cut off mid-document (`Unterminated string`) | `max_tokens` too small for the requested schema | retries with **double** the output budget, bounded by `context_limit` |

Both retries are bounded (`repair_limit`, default 2) and the final error names the real cause:
`provider returned no text content (finish_reason=length, 8192 chars of reasoning, 8192 completion
tokens, model=…) — the model hit its output budget; raise [provider.generation] max_tokens or set
[provider] json_mode = false`.

Recommended settings for reasoning models:

```toml
[provider]
json_mode = false          # skip response_format=json_object

[provider.generation]
max_tokens = 2048          # enough for the planner schema; avoids one wasted round trip
```

`[provider.generation]` is merged verbatim into the request body, so provider-specific knobs (for
example OpenRouter's `reasoning` options) can be set there too.

## Role models

`models.controller`, `models.planner`, `models.evaluator`, `models.verifier` and
`models.escalation` are optional overrides that inherit every unset field from `[provider]`.
Overrides only create a separate provider when at least one field is set, so a single model remains
the default for everything.

`models.escalation` is used for controller decisions after two consecutive rejected steps when
configured; observable signals (repeated failure, stagnation) trigger it, never self-reported model
confidence.

## Planning and evaluation

- `planner.kind = "auto"` uses the model for `InitialPlan` generation when the provider is HTTP;
  user criteria and constraints are merged afterwards and never overwritten.
- `evaluator.kind = "llm"` asks the model for an `Evaluation` with the validation evidence and
  reconstructed context, and can promote failure memories before a rollback.
- `verifier.kind = "hybrid"` keeps deterministic verification for checkable criteria and asks the
  verifier model for criteria that have no deterministic check. The judge sees the objective,
  accepted commit, changed files, validation evidence, a truncated diff and bounded worktree file
  samples; it must return structured `{passed, evidence}`. Provider errors, invalid output, empty
  evidence or `passed = false` all fail verification — semantic judgement can never fabricate
  completion.

## Repair

Malformed structured output gets one bounded repair attempt (a repair prompt with the previous
response and an explicit schema instruction). If it still fails, the run fails cleanly with a
`ProviderOutputError` recorded as an immutable failure memory; there is no unbounded retry loop.
