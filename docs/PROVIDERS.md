# Providers

`FakeProvider` supplies deterministic trajectories and performs one bounded structured-output
repair. `OpenAICompatibleProvider` sends JSON requests to `/chat/completions`, accepts an API key
or environment variable, supports generation parameters and timeout, and retries malformed JSON
only within the configured repair bound. No native tool-calling is required.

`provider.kind = "http"` selects the generic provider; `"openrouter"` is accepted as an alias for
the same endpoint. `"fake"` selects the deterministic offline provider. Any other value is
rejected with an error instead of silently falling back to the fake provider.

The controller composes the model prompt: it contains the allowed tool list with argument
contracts, the `Decision` JSON schema, the action semantics, and the reconstructed context. Models
must return exactly one JSON object. Unknown tools never execute because the runtime validates the
decision with Pydantic and only executes names registered in its registry.

When an API key is configured, the HTTP provider sends it as a standard `Authorization: Bearer`
header. The configured generation mapping is forwarded to the endpoint, with safe defaults for
temperature and maximum output tokens. `context_limit` is also the token budget used when
reconstructing controller context.
