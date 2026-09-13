import json

import httpx
import pytest

from gcae.controller import Controller
from gcae.http_provider import OpenAICompatibleProvider
from gcae.models import Decision
from gcae.providers import DecisionProvider, ProviderOutputError


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


def test_openai_compatible_provider_reports_connection_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://localhost:11434/v1", "model", client=client)
    with pytest.raises(ProviderOutputError, match="connection refused"):
        provider.complete("prompt", Decision)


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


def test_json_mode_is_dropped_when_a_reasoning_model_returns_nothing() -> None:
    """The retry must change the failing input, not repeat it (regression: planner failed)."""
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            # reasoning model: json_object makes it deliberate until the budget is gone
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {"content": "", "reasoning": "think " * 60},
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"completion_tokens": 1024},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish_candidate","semantic_goal":"done",'
                                '"reason_summary":"ok"}'
                            ),
                            "reasoning": "short",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 40},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    decision = provider.complete("prompt", Decision)
    assert decision.action.value == "finish_candidate"
    assert len(payloads) == 2
    assert payloads[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in payloads[1]
    assert "Do not explain" in json.dumps(payloads[1]["messages"])


def test_json_mode_can_be_disabled_from_the_start() -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
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
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, json_mode=False
    )
    provider.complete("prompt", Decision)
    assert len(payloads) == 1
    assert "response_format" not in payloads[0]


def test_empty_response_error_explains_the_real_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": None, "reasoning": "think " * 50},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"completion_tokens": 1024},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "nex-agi/nex", client=client)
    with pytest.raises(ProviderOutputError) as excinfo:
        provider.complete("prompt", Decision)
    message = str(excinfo.value)
    assert "finish_reason=length" in message
    assert "300 chars of reasoning" in message
    assert "1024 completion tokens" in message
    assert "model=nex-agi/nex" in message
    assert "json_mode = false" in message


def test_truncated_json_retries_with_a_larger_budget() -> None:
    """`max_tokens` too small for the schema must escalate, not repeat the same cap."""
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": '{"action":"finish_candidate","semantic_goal":"don'
                            },
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {"completion_tokens": 1024},
                },
            )
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
                        },
                        "finish_reason": "stop",
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    # a configured generation budget must not swallow the escalation
    provider = OpenAICompatibleProvider(
        "http://local/v1",
        "model",
        client=client,
        json_mode=False,
        generation={"max_tokens": 1024, "temperature": 0.0},
    )
    decision = provider.complete("prompt", Decision)
    assert decision.action.value == "finish_candidate"
    assert payloads[0]["max_tokens"] == 1024
    assert payloads[1]["max_tokens"] == 2048
    assert "cut off" in json.dumps(payloads[1]["messages"])


def test_incomplete_json_error_names_the_truncation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": '{"action":"finish_can'},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"completion_tokens": 2048},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, json_mode=False
    )
    with pytest.raises(ProviderOutputError) as excinfo:
        provider.complete("prompt", Decision)
    message = str(excinfo.value)
    assert "incomplete JSON" in message
    assert "finish_reason=length" in message
    assert "2048 completion tokens" in message
