from __future__ import annotations

from .base import GenerationRequest, GenerationResult


class EchoProvider:
    """Deterministic provider used by quick starts and tests."""

    provider_id = "echo"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        latest = request.messages[-1]["content"] if request.messages else ""
        role = request.metadata.get("role_prompt")
        prefix = f"[{role}] " if role else ""
        return GenerationResult(
            text=f"{prefix}Echo: {latest}",
            model_id="echo-v1",
            usage={"input_tokens": len(latest.split()), "output_tokens": len(latest.split()) + 1},
        )
