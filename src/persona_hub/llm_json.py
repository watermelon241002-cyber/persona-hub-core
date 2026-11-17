"""Structured JSON calls for background memory work (reflection, summaries).

These calls never sit on the chat's critical path, so they can afford a
separate, cheaper model and a long timeout.
"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

import httpx


class JsonModel(Protocol):
    async def complete_json(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        """Return one JSON object. Raise on transport or format failure."""


def parse_json_object(content: str) -> dict[str, Any]:
    """Parse a model reply that should be one JSON object.

    Tolerates a markdown fence and stray prose around the object. Anything
    else raises, so a truncated reply is recorded as a failure instead of
    silently becoming an empty result.
    """

    text = (content or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as first_error:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                raise ValueError(f"Model returned invalid JSON: {first_error}") from first_error
        else:
            raise ValueError(f"Model returned invalid JSON: {first_error}") from first_error
    if not isinstance(parsed, dict):
        raise ValueError("Model returned JSON that is not an object")
    return parsed


class OpenAICompatibleJsonModel:
    """JSON-mode chat completions against any OpenAI-compatible endpoint."""

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout_sec: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_sec = timeout_sec

    async def complete_json(
        self,
        *,
        system: str,
        payload: dict[str, Any],
        temperature: float,
        max_tokens: int,
        timeout_sec: float | None = None,
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=float(timeout_sec or self.timeout_sec)) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"authorization": f"Bearer {self.api_key}"},
                json=body,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"JSON model error {response.status_code}: {response.text[:300]}")
        choices = response.json().get("choices") or []
        if not choices:
            raise RuntimeError("JSON model returned no choices")
        return parse_json_object((choices[0].get("message") or {}).get("content") or "")
