from __future__ import annotations

import json
import logging
import os
import random
import time
from collections.abc import Callable
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .providers import ProgressListener, ProviderOutputError, StreamProgress

logger = logging.getLogger("gcae")

# how often a streaming call reports itself while no bytes arrive (hang visibility)
HEARTBEAT_SECONDS = 5.0
# how often incremental progress is reported at most (the runtime throttles events again)
PROGRESS_SECONDS = 0.4
# characters of new content that force a report regardless of the timer
PROGRESS_CHARACTERS = 512

T = TypeVar("T", bound=BaseModel)
_R = TypeVar("_R")


class _StreamUnavailable(RuntimeError):
    """The endpoint refused or dropped a streaming request; retry it without streaming."""


class OpenAICompatibleProvider:
    """Minimal JSON-over-HTTP provider for OpenAI-compatible endpoints."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        api_key_env: str | None = None,
        timeout: float = 60.0,
        context_limit: int = 8192,
        generation: dict[str, Any] | None = None,
        client: httpx.Client | None = None,
        repair_limit: int = 2,
        json_mode: bool = True,
        stream: bool = True,
        stall_timeout: float = 45.0,
        retries: int = 3,
        retry_backoff: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key or (os.getenv(api_key_env) if api_key_env else None)
        self.timeout = timeout
        self.context_limit = context_limit
        self.generation = generation or {}
        self.client = client or httpx.Client(timeout=timeout)
        self._owns_client = client is None
        self.repair_limit = repair_limit
        self.json_mode = json_mode
        # streaming makes a slow model visible token by token, and turns a hang into a
        # detectable event (no bytes for stall_timeout seconds) instead of an indefinite wait
        self.stream = stream
        self.stall_timeout = stall_timeout
        # transient failures (rate limits, 5xx, dropped connections) are retried with
        # exponential backoff before they are allowed to become a run failure
        self.retries = max(0, retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self.on_progress: ProgressListener | None = None
        self._progress_state = (0.0, -1)  # last report time, last reported character count

    def complete(self, prompt: str, schema: type[T]) -> T:
        messages = [{"role": "user", "content": prompt}]
        json_mode = self.json_mode
        budget = self._initial_budget()
        use_stream = self.stream
        for attempt in range(self.repair_limit + 1):
            try:
                body = self._send(
                    messages, json_mode=json_mode, max_tokens=budget, stream=use_stream
                )
            except _StreamUnavailable as exc:
                # the endpoint refused streaming (or dropped it): repeat this attempt
                # without it rather than failing a call that would otherwise work
                if not use_stream:  # pragma: no cover - defensive
                    raise ProviderOutputError(f"provider request failed: {exc}") from exc
                logger.info("streaming unavailable (%s); retrying without it", exc)
                use_stream = False
                continue
            except (httpx.HTTPError, ValueError) as exc:
                raise ProviderOutputError(f"provider request failed: {exc}") from exc
            if isinstance(body, dict) and body.get("error") and not body.get("choices"):
                raise ProviderOutputError(f"provider error: {json.dumps(body['error'])[:200]}")
            last_attempt = attempt >= self.repair_limit
            try:
                raw = self._content(body)
            except ProviderOutputError as exc:
                if last_attempt:
                    raise ProviderOutputError(self._failure_reason(body)) from exc
                # JSON mode makes reasoning models deliberate until the output budget is
                # gone (finish_reason=length, empty content). The prompt already demands
                # JSON and the schema is validated, so retry without the constraint rather
                # than repeating the payload that just failed.
                json_mode = False
                messages = [
                    {"role": "user", "content": prompt},
                    {
                        "role": "user",
                        "content": (
                            "Return only the JSON object now. Do not explain or think out loud."
                        ),
                    },
                ]
                continue
            try:
                return schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                if last_attempt:
                    raise ProviderOutputError(self._failure_reason(body, raw)) from exc
                if self._truncated(body):
                    # the answer was cut off: give it room instead of repeating the cap
                    budget = self._escalated_budget(budget)
                    messages = [
                        {"role": "user", "content": prompt},
                        {
                            "role": "user",
                            "content": (
                                "Your previous answer was cut off before the JSON was complete. "
                                "Return the complete JSON object and nothing else."
                            ),
                        },
                    ]
                else:
                    messages = [
                        {"role": "user", "content": prompt},
                        {"role": "assistant", "content": raw},
                        {
                            "role": "user",
                            "content": (
                                "Return only valid JSON matching the requested schema. "
                                "Repair the previous output."
                            ),
                        },
                    ]
                continue
        raise ProviderOutputError("provider repair loop exhausted")

    # ---------------------------------------------------------------- transport

    def _transient(self, exc: BaseException) -> tuple[bool, float]:
        """Is this worth retrying, and after how long?"""
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 429 or 500 <= status < 600:
                retry_after = exc.response.headers.get("retry-after", "")
                try:
                    return True, max(0.0, float(retry_after))
                except ValueError:
                    return True, 0.0
            return False, 0.0
        if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
            return True, 0.0
        return False, 0.0

    def _with_retries(self, attempt_call: Callable[[], _R]) -> _R:
        """Run one request, retrying transient failures with backoff and jitter."""
        last: BaseException | None = None
        for index in range(self.retries + 1):
            try:
                return attempt_call()
            except Exception as exc:  # noqa: BLE001 - classified immediately below
                retryable, retry_after = self._transient(exc)
                if not retryable or index >= self.retries:
                    raise
                last = exc
                delay = retry_after or min(30.0, self.retry_backoff * (2**index))
                delay += random.uniform(0, min(1.0, delay / 4 or 0.5))
                logger.warning(
                    "provider request failed (%s: %s); retry %d/%d in %.1fs",
                    type(exc).__name__, str(exc)[:120], index + 1, self.retries, delay,
                )
                if self.on_progress is not None:
                    self._report([], [], time.monotonic(), waiting=True)
                time.sleep(delay)
        raise ProviderOutputError(f"provider request failed after retries: {last}")


    def _send(
        self,
        messages: list[dict[str, str]],
        json_mode: bool,
        max_tokens: int | None,
        stream: bool,
    ) -> Any:
        """One request. Returns the same body shape for streamed and buffered replies."""
        payload = self._payload(messages, json_mode=json_mode, max_tokens=max_tokens)
        url = f"{self.base_url}/chat/completions"
        if stream:
            return self._with_retries(lambda: self._streamed(url, payload))
        # read=stall_timeout: a silent endpoint fails in seconds with a truthful reason
        # instead of holding the run for the whole request budget
        timeout = httpx.Timeout(
            self.timeout, connect=min(15.0, self.timeout), read=max(1.0, self.stall_timeout)
        )
        try:
            response = self._with_retries(
                lambda: self._post(url, payload, timeout)
            )
            return response.json()
        except httpx.TimeoutException as exc:
            raise ProviderOutputError(
                f"provider request stalled: no data for {self.stall_timeout:.0f}s "
                f"(model={self.model}) — the endpoint accepted the request and produced nothing"
            ) from exc

    def _post(self, url: str, payload: dict[str, Any], timeout: httpx.Timeout) -> httpx.Response:
        response = self.client.post(
            url, headers=self._headers(), json=payload, timeout=timeout
        )
        response.raise_for_status()
        return response

    def _streamed(self, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Stream a completion, reporting progress, and rebuild a buffered-style body.

        Nothing arrives for ``stall_timeout`` seconds: the call is aborted with an explicit
        error instead of leaving the run waiting forever (the runtime then hands it to the
        recovery ladder). Progress is reported to ``on_progress`` when a listener is set.
        """
        request = {**payload, "stream": True}
        text: list[str] = []
        reasoning: list[str] = []
        finish: str | None = None
        usage: dict[str, Any] = {}
        started = time.monotonic()
        last_data = started
        read_timeout = max(1.0, self.stall_timeout)
        timeout = httpx.Timeout(
            max(self.timeout, read_timeout),
            connect=min(15.0, max(self.timeout, read_timeout)),
            read=read_timeout,
        )
        try:
            with self.client.stream(
                "POST", url, headers=self._headers(), json=request, timeout=timeout
            ) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if "text/event-stream" not in content_type:
                    # the endpoint ignored stream=true and answered with a buffered body; take
                    # it as it is instead of reporting an empty stream
                    logger.info("endpoint answered %r to a streaming request", content_type)
                    body = response.json()
                    if not isinstance(body, dict):
                        raise ProviderOutputError("provider response was not an object")
                    return body
                self._report(text, reasoning, started, waiting=True)
                lines = response.iter_lines()
                while True:
                    try:
                        line = next(lines)
                    except StopIteration:
                        break
                    except httpx.TimeoutException as exc:
                        # no bytes for the whole stall budget: report it instead of waiting
                        # forever. The run hands this to the recovery ladder, which can ask
                        # the user or retry with another model.
                        idle = time.monotonic() - last_data
                        raise ProviderOutputError(
                            f"provider stream stalled: no data for {idle:.0f}s "
                            f"({len(''.join(text))} characters received, model={self.model})"
                            " — the endpoint stopped producing output"
                        ) from exc
                    last_data = time.monotonic()
                    if not line:
                        continue
                    if line.startswith("data:"):
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        self._consume_chunk(data, text, reasoning, usage)
                        finish = self._chunk_finish(data) or finish
                        self._report(text, reasoning, started)
        except httpx.TimeoutException as exc:
            # the endpoint accepted the connection and stayed silent: report the stall now
            # instead of repeating it as a buffered request
            raise ProviderOutputError(
                f"provider request stalled: no data for {self.stall_timeout:.0f}s "
                f"(model={self.model}) — the endpoint accepted the request and produced nothing"
            ) from exc
        except httpx.HTTPStatusError as exc:
            if self._transient(exc)[0]:
                raise  # rate limit / server error: retry the stream, do not downgrade it
            raise _StreamUnavailable(f"HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise _StreamUnavailable(str(exc)) from exc
        self._report(text, reasoning, started, force=True)
        content = "".join(text)
        return {
            "choices": [
                {
                    "finish_reason": finish,
                    "message": {
                        "content": content,
                        "reasoning": "".join(reasoning),
                    },
                }
            ],
            "usage": usage,
        }

    @staticmethod
    def _consume_chunk(
        data: str,
        text: list[str],
        reasoning: list[str],
        usage: dict[str, Any],
    ) -> None:
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            return  # a single malformed SSE frame must not kill a stream that may recover
        if not isinstance(chunk, dict):
            return
        if chunk.get("error"):
            raise ProviderOutputError(f"provider error: {json.dumps(chunk['error'])[:200]}")
        if isinstance(chunk.get("usage"), dict):
            usage.update(chunk["usage"])
        choices = chunk.get("choices") or []
        if not choices:
            return
        delta = choices[0].get("delta") or choices[0].get("message") or {}
        piece = delta.get("content")
        if isinstance(piece, str) and piece:
            text.append(piece)
        elif isinstance(piece, list):
            text.append(
                "".join(
                    part["text"]
                    for part in piece
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                )
            )
        thought = delta.get("reasoning") or delta.get("reasoning_content")
        if isinstance(thought, str) and thought:
            reasoning.append(thought)

    @staticmethod
    def _chunk_finish(data: str) -> str | None:
        try:
            choices = json.loads(data).get("choices") or []
        except (json.JSONDecodeError, AttributeError):
            return None
        if not choices:
            return None
        finish = choices[0].get("finish_reason")
        return finish if isinstance(finish, str) else None

    def _report(
        self,
        text: list[str],
        reasoning: list[str],
        started: float,
        waiting: bool = False,
        force: bool = False,
    ) -> None:
        """Call the progress listener, bounded in rate and never able to break the call."""
        listener = self.on_progress
        if listener is None:
            return
        now = time.monotonic()
        characters = len("".join(text))
        last_report, last_characters = self._progress_state
        if not force and not waiting and now - last_report < PROGRESS_SECONDS:
            if characters - last_characters < PROGRESS_CHARACTERS:
                return
        self._progress_state = (now, characters)
        update = StreamProgress(
            characters=characters,
            reasoning_characters=sum(len(part) for part in reasoning),
            elapsed_ms=int((now - started) * 1000),
            waiting=waiting,
            preview=" ".join("".join(text).split())[-160:],
        )
        try:
            listener(update)
        except Exception:  # noqa: BLE001 - a listener must never break the model call
            logger.exception("provider progress listener failed")

    def _payload(
        self,
        messages: list[dict[str, str]],
        json_mode: bool | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"model": self.model, "messages": messages}
        enabled = self.json_mode if json_mode is None else json_mode
        if enabled:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.generation)
        payload.setdefault("temperature", 0.0)
        if max_tokens is not None:
            # the per-attempt budget (possibly escalated after truncation) wins over config
            payload["max_tokens"] = max_tokens
        elif "max_tokens" not in payload:
            payload["max_tokens"] = self._initial_budget()
        return payload

    def _initial_budget(self) -> int:
        configured = self.generation.get("max_tokens")
        if isinstance(configured, int) and configured > 0:
            return configured
        return min(1024, self.context_limit)

    def _escalated_budget(self, budget: int) -> int:
        """Doubling step for a truncated answer, bounded by the configured context limit."""
        ceiling = max(2048, min(self.context_limit, budget * 4))
        return int(min(ceiling, budget * 2))

    @staticmethod
    def _truncated(body: Any) -> bool:
        try:
            finish = body["choices"][0].get("finish_reason")
        except (KeyError, IndexError, TypeError, AttributeError):
            return False
        return bool(finish == "length")

    def _failure_reason(self, body: Any, raw: str | None = None) -> str:
        """Explain a failed response with the fields that actually came back."""
        details: list[str] = []
        finish = None
        reasoning = ""
        try:
            choice = body["choices"][0]
            finish = choice.get("finish_reason")
            message = choice.get("message") or {}
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError):
            pass
        completion_tokens = (body.get("usage") or {}).get("completion_tokens")
        if finish:
            details.append(f"finish_reason={finish}")
        if raw:
            details.append(f"{len(raw)} chars returned")
        if isinstance(reasoning, str) and reasoning:
            details.append(f"{len(reasoning)} chars of reasoning")
        if completion_tokens:
            details.append(f"{completion_tokens} completion tokens")
        details.append(f"model={self.model}")
        kind = "provider returned incomplete JSON" if raw else "provider returned no text content"
        hint = ""
        if finish == "length" or reasoning:
            hint = (
                " — the model hit its output budget; raise [provider.generation] max_tokens "
                "or set [provider] json_mode = false"
            )
        elif raw:
            hint = " — the model did not return the requested schema"
        return f"{kind} ({', '.join(details)}){hint}"

    @classmethod
    def _content(cls, payload: Any) -> str:
        """Message text, rejecting blank content ('' counts as no content, not as JSON)."""
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderOutputError("provider response did not contain text content") from exc
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            )
        else:
            text = ""
        if not text.strip():
            raise ProviderOutputError("provider response did not contain text content")
        return text

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
