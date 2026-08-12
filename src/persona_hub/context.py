from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .db import Database
from .memory import MemoryGateway, RecalledMemory


@dataclass(frozen=True, slots=True)
class ContextPackage:
    system_prompt: str
    messages: list[dict[str, str]]
    recalled_memories: list[RecalledMemory]


class ContextBuilder:
    def __init__(
        self,
        *,
        database: Database,
        memory_gateway: MemoryGateway,
        persona_file: Path,
        recent_message_limit: int = 24,
    ):
        self.database = database
        self.memory_gateway = memory_gateway
        self.persona_file = persona_file
        self.recent_message_limit = recent_message_limit

    def _persona_text(self) -> str:
        try:
            return self.persona_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "You are a careful, continuous assistant."

    def build(self, conversation_id: str, current_content: str) -> ContextPackage:
        conversation = self.database.get_conversation(conversation_id)
        history = self.database.list_messages(
            conversation_id, limit=self.recent_message_limit
        )
        recalled = self.memory_gateway.recall(current_content, limit=5)

        stable = self._persona_text()
        dynamic_sections: list[str] = []
        if conversation["rolling_summary"]:
            dynamic_sections.append(
                "Conversation summary:\n" + conversation["rolling_summary"]
            )
        if recalled:
            memory_lines = [f"- [{item.kind}] {item.content}" for item in recalled]
            dynamic_sections.append("Relevant memories:\n" + "\n".join(memory_lines))

        system_prompt = stable
        if dynamic_sections:
            system_prompt += "\n\n" + "\n\n".join(dynamic_sections)

        messages = [
            {"role": row["role"], "content": row["content"]}
            for row in history
            if row["role"] in {"user", "assistant"}
        ]
        return ContextPackage(
            system_prompt=system_prompt,
            messages=messages,
            recalled_memories=recalled,
        )
