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
        repair_limit: int = 1,
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

    def complete(self, prompt: str, schema: type[T]) -> T:
        messages = [{"role": "user", "content": prompt}]
        for attempt in range(self.repair_limit + 1):
            try:
                response = self.client.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._headers(),
                    json=self._payload(messages),
                )
                response.raise_for_status()
                raw = self._content(response.json())
            except (httpx.HTTPError, KeyError, IndexError, TypeError) as exc:
                raise ProviderOutputError("provider request failed") from exc
            try:
                return schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
                if attempt >= self.repair_limit:
                    raise ProviderOutputError(
                        "provider returned invalid structured output"
                    ) from exc
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
        raise ProviderOutputError("provider repair loop exhausted")

    def _payload(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        payload.update(self.generation)
        payload.setdefault("temperature", 0.0)
        payload.setdefault("max_tokens", min(1024, self.context_limit))
        return payload

    @staticmethod
    def _content(payload: Any) -> str:
        content = payload["choices"][0]["message"]["content"]
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                item["text"]
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            if text_parts:
                return "".join(text_parts)
        raise ProviderOutputError("provider response did not contain text content")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self) -> None:
        if self._owns_client:
            self.client.close()
