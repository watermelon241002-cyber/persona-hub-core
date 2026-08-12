from __future__ import annotations

import httpx

from .base import GenerationRequest, GenerationResult


class OpenAICompatibleProvider:
    provider_id = "openai-compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str, timeout: float = 120.0):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if not self.base_url or not self.api_key or not self.model:
            raise RuntimeError("OpenAI-compatible provider is not configured")

        messages = []
        if request.system_prompt:
            messages.append({"role": "system", "content": request.system_prompt})
        messages.extend(request.messages)

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={"model": self.model, "messages": messages, "stream": False},
            )
            response.raise_for_status()
            data = response.json()

        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        return GenerationResult(
            text=text,
            model_id=data.get("model", self.model),
            usage={str(key): int(value) for key, value in usage.items() if isinstance(value, int)},
        )
