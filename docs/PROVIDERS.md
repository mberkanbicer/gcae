# Providers

`FakeProvider` supplies deterministic trajectories and performs one bounded structured-output repair. `OpenAICompatibleProvider` sends JSON requests to `/chat/completions`, accepts an API key or environment variable, supports generation parameters and timeout, and retries malformed JSON only within the configured repair bound. No native tool-calling is required.

When an API key is configured, the HTTP provider sends it as a standard `Authorization: Bearer`
header. The configured generation mapping is forwarded to the endpoint, with safe defaults for
temperature and maximum output tokens.
