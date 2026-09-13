# Providers

One generic OpenAI-compatible provider serves OpenRouter, Ollama, vLLM, LM Studio and similar
endpoints. `provider.kind = "http"` or `"openrouter"` selects it; `"fake"` selects the deterministic
offline provider. Unknown kinds are rejected — never silently replaced.

Configurable per provider: `base_url`, `model`, `api_key` or `api_key_env`, `timeout`,
`context_limit` and `generation` parameters. The HTTP provider posts to `/chat/completions` with
`response_format: json_object`, forwards the generation mapping and sends `Authorization: Bearer`
when a key is configured. Native tool calling is not used: the model returns structured JSON that
the runtime validates and executes.

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
