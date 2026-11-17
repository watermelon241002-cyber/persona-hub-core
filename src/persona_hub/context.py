from __future__ import annotations

from dataclasses import dataclass
from datetime import timezone, tzinfo
import json
from pathlib import Path

from .db import Database
from .memory import MemoryGateway, RecalledMemory
from .memory_summaries import SENSITIVE_TAG
from .memory_text import local_time_text, redact_credentials


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
        tz: tzinfo = timezone.utc,
        daily_summary_limit: int = 2,
    ):
        self.database = database
        self.memory_gateway = memory_gateway
        self.persona_file = persona_file
        self.recent_message_limit = recent_message_limit
        self.tz = tz
        self.daily_summary_limit = daily_summary_limit

    def _persona_text(self) -> str:
        try:
            return self.persona_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return "You are a careful, continuous assistant."

    def _recent_summaries(self) -> list[str]:
        """Latest weekly and daily summaries, oldest first.

        Rows tagged sensitive stay in storage but are not volunteered into
        every prompt.
        """

        picked: list[dict] = []
        for summary_type, limit in (("weekly", 1), ("daily", self.daily_summary_limit)):
            if limit <= 0:
                continue
            rows = self.database.list_memory_summaries(summary_type, limit=limit + 5)
            visible = [row for row in rows if SENSITIVE_TAG not in json.loads(row["tags_json"] or "[]")]
            picked.extend(visible[:limit])
        picked.sort(key=lambda row: row["period_start"])
        lines = []
        for row in picked:
            day = local_time_text(row["period_start"], self.tz, "%Y-%m-%d")
            line = f"- [{row['summary_type']} from {day}] {row['content']}"
            if row["scene"]:
                line += f" (scene: {row['scene']})"
            quotes = json.loads(row["quotes_json"] or "[]")
            if quotes:
                line += " Lines: " + " / ".join(f"“{quote}”" for quote in quotes[:4])
            lines.append(redact_credentials(line))
        return lines

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
        summaries = self._recent_summaries()
        if summaries:
            dynamic_sections.append("Recent days:\n" + "\n".join(summaries))
        if recalled:
            memory_lines = [
                redact_credentials(f"- [{item.kind}] {item.content}") for item in recalled
            ]
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
