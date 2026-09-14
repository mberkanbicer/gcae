# Providers

GCAE speaks the OpenAI-compatible chat completions API, which covers OpenRouter, Ollama, LM Studio,
vLLM and most hosted gateways.

## Minimal configuration

```toml
[provider]
kind = "openrouter"
base_url = "https://openrouter.ai/api/v1"
model = "qwen/qwen3-coder"
api_key_env = "OPENROUTER_API_KEY"

[provider.generation]
temperature = 0.0
max_tokens = 2048
```

A local server needs no key (`base_url = "http://localhost:11434/v1"`). `kind = "fake"` is a
built-in deterministic provider used by the test suite and the demo scripts; it is the default when
no configuration is found, which is why GCAE warns instead of failing obscurely.

## What GCAE sends

Every call is a fresh, reconstructed prompt: the objective, hard constraints, success criteria, the
current goal, the accepted commit, relevant memory and the recent trace. Prompts are *not* a growing
conversation, and pinned data cannot be budgeted away. Output must match a Pydantic schema; invalid
output is repaired a bounded number of times, then fails the call (and the run's recovery ladder
takes over) rather than being guessed at.

## Reasoning models

Reasoning models can spend the whole output budget thinking and return empty content. If that
happens:

- `json_mode = false` (some models produce empty content when forced into JSON mode);
- raise `max_tokens` (2048 is enough for the planner schema);
- keep the model for `[models.recovery]` / `[models.escalation]` where thinking helps, and use a
  faster model for the controller and planner.

## Streaming, stalls and retries

| Behaviour | Detail |
| --- | --- |
| Streaming | on by default; the runtime records first-token latency, characters, reasoning characters and a heartbeat every 10s |
| Buffered fallback | an endpoint that answers without a stream is still accepted |
| Stall detection | no data for `stall_timeout` fails with `provider request stalled: …`, never an indefinite wait |
| Retries | 429 / 5xx / timeouts / dropped connections retry with exponential backoff, jitter and `Retry-After` support |
| No retry | 400/404/422 — configuration problems; the recovery ladder is the better answer than repetition |

## Failover and escalation

A dead or misconfigured model is the one failure self-diagnosis cannot fix (the advisor would have to
call the same endpoint), so `[models.escalation]` is used instead:

| Trigger | Effect |
| --- | --- |
| controller, planner, evaluator or verifier call fails | that role moves to the fallback model once (`model_failover`) |
| the same rejection repeats three times | the controller escalates (`model_escalated`) before the run would ask you |
| the evaluator's model fails | the *same* work is judged again on the fallback instead of being thrown away |

Verified live: a deliberately invalid controller model id → `model_failover` → the task completed in
11 seconds.

## Verifier kinds

`deterministic` runs the criteria as code. `hybrid` adds a model judge for criteria that cannot be
expressed exactly; the judge fails **closed** — an unparseable or uncertain judgement is a failure,
never a pass.

## Provider troubleshooting

| Symptom | Explanation |
| --- | --- |
| `provider output: …` and the run stops | the model produced unusable output repeatedly; the run's diagnosis is in `gcae inspect` |
| `provider request stalled` / `provider stream stalled` | the endpoint accepted the request and produced nothing within `stall_timeout` |
| empty content, no error | reasoning model with a small output budget — see above |
| `no API key configured for …` | set `provider.api_key` or `provider.api_key_env` |
| every call fails instantly | wrong model id or base URL; check with a single `curl` first |
