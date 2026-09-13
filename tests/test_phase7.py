import json

import httpx

from gcae.controller import Controller
from gcae.http_provider import OpenAICompatibleProvider
from gcae.models import Decision
from gcae.providers import DecisionProvider


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
                                '{"action":"finish_candidate","semantic_goal":"done","reason_summary":"ok"}'
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
    assert decision.action.value == "finish_candidate"
    assert calls == 1
    assert received["temperature"] == 0.2
    assert received["top_p"] == 0.8
    assert authorization == "Bearer secret"


def test_openai_compatible_provider_retries_empty_content() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish_candidate","semantic_goal":"done",'
                                '"reason_summary":"ok"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    decision = provider.complete("prompt", Decision)
    assert decision.action.value == "finish_candidate"
    assert calls == 2


class RecordingProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, prompt: str, schema: type[Decision]) -> Decision:
        del schema
        self.prompts.append(prompt)
        return Decision(action="replan", semantic_goal="goal", reason_summary="reason")


def test_controller_prompt_includes_decision_contract() -> None:
    provider = RecordingProvider()
    controller = Controller(DecisionProvider(provider), ["read_file", "run_command"])
    decision = controller.decide("CTX")
    assert decision.action.value == "replan"
    prompt = provider.prompts[0]
    assert "execute_tool" in prompt
    assert "Decision JSON schema" in prompt
    assert "read_file" in prompt
    assert "run_command" in prompt
    assert "CTX" in prompt
