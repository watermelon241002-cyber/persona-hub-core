from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class GenerationRequest:
    system_prompt: str
    messages: list[dict[str, str]]
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GenerationResult:
    text: str
    model_id: str
    usage: dict[str, int] = field(default_factory=dict)


class Provider(Protocol):
    provider_id: str

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate exactly one persisted logical response."""
