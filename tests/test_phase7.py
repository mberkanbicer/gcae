import json

import httpx
import pytest

from gcae.controller import Controller
from gcae.http_provider import OpenAICompatibleProvider
from gcae.models import Decision
from gcae.providers import DecisionProvider, ProviderOutputError, StreamProgress


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


class _Sse(httpx.SyncByteStream):
    """Minimal SSE byte stream, optionally stalling after the first frame."""

    def __init__(
        self, frames: list[str], stall_after: int | None = None, done: bool = True
    ) -> None:
        self.frames = frames
        frames = [*frames, "[DONE]"] if done else list(frames)
        self.payload = "".join(f"data: {frame}\n\n" for frame in frames).encode()
        self.offset = 0
        self.stall_after = stall_after
        self.delivered = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        return self

    def __next__(self) -> bytes:
        if self.stall_after is not None and self.delivered >= self.stall_after:
            raise httpx.ReadTimeout("no data")
        if self.offset >= len(self.payload):
            raise StopIteration
        self.delivered += 1
        chunk = self.payload[self.offset : self.offset + 256]
        self.offset += 256
        return chunk

    def close(self) -> None:
        return None


def _sse_response(
    frames: list[str], stall_after: int | None = None, done: bool = True
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=_Sse(frames, stall_after, done),
    )


def _delta(text: str, reasoning: str = "") -> str:
    payload: dict[str, object] = {"choices": [{"delta": {"content": text}}]}
    if reasoning:
        payload["choices"] = [{"delta": {"reasoning": reasoning}}]  # type: ignore[list-item]
    return json.dumps(payload)


def test_streaming_provider_assembles_content_and_reports_progress() -> None:
    head = '{"action":"finish_candidate",'
    tail = '"semantic_goal":"done","reason_summary":"streamed"}'
    frames = [
        json.dumps({"choices": [{"delta": {"reasoning": "thinking hard "}}]}),
        _delta(head),
        _delta(tail),
        json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}]}),
    ]
    seen: list[object] = []

    def listener(update: object) -> None:
        seen.append(update)

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return _sse_response(frames)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    provider.on_progress = listener
    decision = provider.complete("prompt", Decision)

    assert decision.action.value == "finish_candidate"
    assert decision.reason_summary == "streamed"
    updates = [item for item in seen if isinstance(item, StreamProgress)]
    assert len(updates) >= 2, "a stream must report progress while it arrives"
    assert updates[0].waiting is True, "the open connection is reported before the first token"
    # the final (forced) report carries the finished size, so the dashboard can show it
    assert updates[-1].characters == len(head) + len(tail)
    assert updates[-1].reasoning_characters > 0
    assert provider.on_progress is listener  # the runtime clears it, not the provider


def test_streaming_falls_back_when_the_endpoint_refuses_streaming() -> None:
    calls: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        streamed = json.loads(request.content).get("stream") is True
        calls.append(streamed)
        if streamed:
            return httpx.Response(400, json={"error": {"message": "stream unsupported"}})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish_candidate","semantic_goal":"done",'
                                '"reason_summary":"buffered"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    decision = provider.complete("prompt", Decision)

    assert decision.reason_summary == "buffered"
    assert calls == [True, False], "one streaming attempt, then a buffered one"


def test_streaming_handles_an_endpoint_that_ignores_stream_true() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish_candidate","semantic_goal":"done",'
                                '"reason_summary":"ignored the flag"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    decision = provider.complete("prompt", Decision)
    assert decision.reason_summary == "ignored the flag"


def test_a_stalled_stream_is_detected_and_heartbeats_are_reported() -> None:
    seen: list[object] = []

    def listener(update: object) -> None:
        seen.append(update)

    def handler(request: httpx.Request) -> httpx.Response:
        # the first frame arrives whole, then the endpoint goes silent forever
        return _sse_response([_delta("partial")], stall_after=1, done=False)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, stall_timeout=0.05
    )
    provider.on_progress = listener
    with pytest.raises(ProviderOutputError) as caught:
        provider.complete("prompt", Decision)

    message = str(caught.value)
    assert "stalled" in message
    # what already arrived is reported, not swallowed: the message names it, and the caller
    # can decide to retry or hand the run to the recovery ladder
    assert "7 characters received" in message
    updates = [item for item in seen if isinstance(item, StreamProgress)]
    assert any(item.waiting for item in updates), "a hang must be visible while it happens"


def test_malformed_stream_frames_do_not_kill_a_recovering_stream() -> None:
    frames = [
        "not json at all",
        _delta('{"action":"finish_candidate","semantic_goal":"done",'),
        "{}",
        _delta('"reason_summary":"ok"}'),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _sse_response(frames)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "model", client=client)
    decision = provider.complete("prompt", Decision)
    assert decision.reason_summary == "ok"


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


def test_transient_rate_limits_are_retried_with_backoff() -> None:
    """A 429 is not a run failure: it is a wait, then a retry."""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) <= 2:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"action":"finish_candidate","semantic_goal":"done",'
                                '"reason_summary":"after retries"}'
                            )
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, retries=3, retry_backoff=0.01
    )
    decision = provider.complete("prompt", Decision)
    assert decision.reason_summary == "after retries"
    assert len(calls) == 3, f"expected two retries, saw {len(calls)} attempts"


def test_persistent_rate_limits_fail_after_the_retry_budget() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429, json={"error": "slow down"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, retries=2, retry_backoff=0.01
    )
    with pytest.raises(ProviderOutputError):
        provider.complete("prompt", Decision)
    assert len(calls) == 3, "two retries then give up: the run's ladder takes over from there"


def test_a_client_error_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={"error": {"message": "bad request"}})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider(
        "http://local/v1", "model", client=client, retries=3, retry_backoff=0.01
    )
    with pytest.raises(ProviderOutputError):
        provider.complete("prompt", Decision)
    # one streaming attempt, then one buffered fallback: a bad request is not worth backing off
    assert len(calls) == 2, f"a 400 must not be retried, saw {len(calls)} attempts"
