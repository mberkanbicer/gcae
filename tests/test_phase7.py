import json

import httpx

from gcae.http_provider import OpenAICompatibleProvider
from gcae.models import Decision


def test_openai_compatible_provider() -> None:
    calls = 0
    received: dict[str, object] = {}
    authorization = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal authorization, calls
        calls += 1
        received.update(json.loads(request.content))
        authorization = request.headers["Authorization"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish","semantic_goal":"done","reason_summary":"ok"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1",
        "model",
        api_key="secret",
        generation={"temperature": 0.2, "top_p": 0.8},
        client=client,
    )
    decision = provider.complete("prompt", Decision)
    assert decision.action.value == "finish"
    assert calls == 1
    assert received["temperature"] == 0.2
    assert received["top_p"] == 0.8
    assert authorization == "Bearer secret"
