from __future__ import annotations

import json
import os
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .providers import ProviderOutputError

T = TypeVar("T", bound=BaseModel)


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

    def complete(self, prompt: str, schema: type[T]) -> T:
        messages = [{"role": "user", "content": prompt}]
        json_mode = self.json_mode
        budget = self._initial_budget()
        for attempt in range(self.repair_limit + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._payload(messages, json_mode=json_mode, max_tokens=budget),
                )
                response.raise_for_status()
                body = response.json()
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
