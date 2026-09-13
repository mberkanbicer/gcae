# Providers

GCAE speaks the OpenAI chat-completions contract. That covers Ollama, llama.cpp server, LM Studio,
vLLM, OpenRouter, and most hosted endpoints.

## Minimal configuration

```toml
[provider]
kind = "http"                            # or "openrouter"
base_url = "http://localhost:11434/v1"
model = "qwen2.5-coder:14b"
api_key_env = "OPENROUTER_API_KEY"       # only for remote endpoints
context_limit = 8192
timeout = 60
```

`kind = "fake"` is a deterministic offline provider used by the test suite and by
`tools/tui_demo.py`; it never calls the network.

## What GCAE sends

- `messages`: one user message containing the reconstructed context.
- `response_format: {"type": "json_object"}` unless `json_mode = false`.
- `temperature` (default `0.0`), `max_tokens` (default `min(1024, context_limit)`).
- everything in `[provider.generation]`, merged verbatim, so provider-specific options
  (for example OpenRouter's `reasoning` block) can be set there.

Every reply is validated against a pydantic schema. A reply that does not match is re-requested with
a bounded repair budget; the run never proceeds on free-form text.

## Reasoning models

Reasoning models can spend the entire output budget deliberating. GCAE detects both failure modes and
recovers:

| Symptom | Cause | Behaviour |
| --- | --- | --- |
| empty `content`, `finish_reason=length`, large `reasoning` | JSON mode makes the model deliberate to the token limit | retried **without** `response_format` |
| valid JSON start, cut off mid-document | `max_tokens` too small for the requested schema | retried with **double** the output budget, bounded by `context_limit` |

Recommended for reasoning models:

```toml
[provider]
json_mode = false

[provider.generation]
max_tokens = 2048      # the planner schema needs roughly 1500
```

Failures name the real cause, for example:

```
provider returned no text content (finish_reason=length, 36648 chars of reasoning,
8192 completion tokens, model=…) — the model hit its output budget; raise
[provider.generation] max_tokens or set [provider] json_mode = false
```

## Roles and escalation

```toml
[models.escalation]
model = "a/stronger-model"
```

The escalation role is used after repeated failures and once when a run stagnates, before the run
asks you. Other roles (`planner`, `evaluator`, `verifier`) can be overridden the same way.

## Verifier kinds

`[verifier] kind = "deterministic"` (default) only accepts criteria GCAE can check itself.
`kind = "hybrid"` adds a model judge for prose criteria; the judge must answer with structured
evidence and fails closed on provider errors, empty evidence, or a negative verdict. The judge never
overrides a deterministic failure.

## Provider troubleshooting

| Symptom | Fix |
| --- | --- |
| `no API key configured for …` | set `api_key` or `api_key_env`, or use a local endpoint |
| `Connection refused` | start the local server, or fix `base_url` |
| `404` | wrong model name or wrong `base_url` path (`/v1/chat/completions` is appended) |
| endless empty responses | set `json_mode = false` and raise `max_tokens` (see above) |
| very slow runs | lower `context_limit`, pick a smaller model, or use `[planner] kind = "deterministic"` |
