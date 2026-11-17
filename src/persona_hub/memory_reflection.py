"""Turn reflection: after each exchange, decide what (if anything) is worth keeping.

Runs in the background after the reply has been delivered. It never blocks a
chat turn and never fails one.

Lessons from production that shaped this module:

- **Give the reflector the previous few turns.** Real conversation jumps
  around. "Is that done yet?" cannot be understood alone, and a model that
  cannot resolve the reference falls back to vague paraphrase.
- **Anchor the score.** Without anchors every candidate comes back at 0.95,
  and the long-term store fills with ordinary affection and small talk.
  "Nothing worth keeping" must be an explicitly allowed, common answer.
- **Do not let one kind swallow the rest.** Left alone, a model files almost
  everything under "emotion". Facts, preferences, work progress, plans and
  relationship settings each have their own kind.
- **No report voice.** "Both parties engaged in deep emotional confirmation"
  is useless to a future self. Write who said what; keep strong short lines
  verbatim as quotes.
- **Hiding is only on explicit request.** Intimate or explicit content is not
  by itself a reason to hide anything; only the user saying "don't keep this"
  is.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any, Iterable

from .db import Database
from .llm_json import JsonModel
from .memory import MemoryGateway
from .memory_text import (
    clean_quote,
    clean_tags,
    float_range,
    optional_float_range,
    text_similarity,
    trim,
)

MAX_CANDIDATES_PER_TURN = 4
# Per-kind ceiling inside one turn. Unknown kinds get two.
CANDIDATE_PER_KIND_LIMIT = {
    "quote": 1,
    "emotion": 1,
    "relationship": 1,
    "fact": 1,
    "preference": 1,
    "identity": 1,
    "habit": 1,
    "work": 1,
}
DUPLICATE_SIMILARITY = 0.85
RECENT_CONTEXT_TURNS = 4

DEFAULT_DO_NOT_SURFACE_MARKERS = (
    "这个别存",
    "这句别存",
    "这段别存",
    "别记这个",
    "不要记这个",
    "不要存这个",
    "别让这个浮现",
    "don'tsavethis",
    "donotsavethis",
    "don'trememberthis",
    "donotrememberthis",
    "don'tkeepthis",
)

_COMPACT_RE = re.compile(r"[\s，。！？、,.!?\"'“”‘’：:；;（）()\[\]【】<>《》\-—_]+")
_NOISE_MARKERS = ("测试", "test", "打招呼", "寒暄", "闲聊", "无长期", "没有长期", "无实质", "临时")
_STABLE_KINDS = frozenset({"fact", "preference", "identity", "habit", "work"})


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    """Where a reflected candidate goes: dropped, queued for review, or kept.

    Thresholds are data. Note the shape they create with the defaults: a
    stable fact is kept at 0.82 but only *queued* from 0.93, so anything in
    between is dropped and the review queue mostly stays empty. That is the
    production behaviour, documented rather than hidden; tune it if you want a
    busier queue.
    """

    drop_below: float = 0.55
    min_compact_chars: int = 8
    filler_words: tuple[str, ...] = ()
    party_names: tuple[str, ...] = ()

    def should_drop(self, content: str, score: float) -> bool:
        compact = _COMPACT_RE.sub("", content.lower())
        if score < self.drop_below or len(compact) < self.min_compact_chars:
            return True
        if any(marker in compact for marker in _NOISE_MARKERS):
            return True
        fillers = ["嗯+", "啊+", "哦+", "好+", "哈哈+", "嘿嘿+", "ok+", "lol+"]
        fillers.extend(f"(?:{re.escape(word.lower())})+" for word in self.filler_words if word)
        return bool(re.fullmatch(f"(?:{'|'.join(fillers)})+", compact))

    def should_keep(self, *, content: str, kind: str, tags: Iterable[str], score: float, importance: float) -> bool:
        """`importance` is on the reflector's 1..10 scale."""

        tag_set = set(tags)
        if kind == "quote" or "quote" in tag_set:
            return score >= 0.97 and importance >= 8
        if kind == "emotion" or "emotion" in tag_set:
            return score >= 0.96 and importance >= 7
        if kind == "relationship" or "relationship" in tag_set:
            return score >= 0.95 and importance >= 8
        if kind in _STABLE_KINDS or tag_set & _STABLE_KINDS:
            return score >= 0.82
        if kind == "plan" or "plan" in tag_set:
            return score >= 0.9 and importance >= 7
        return score >= 0.95 and any(name and name in content for name in self.party_names)

    def should_queue(self, *, kind: str, tags: Iterable[str], score: float) -> bool:
        tag_set = set(tags)
        if kind == "quote" or "quote" in tag_set:
            return score >= 0.96
        if kind == "emotion" or "emotion" in tag_set:
            return score >= 0.95
        if kind == "relationship" or "relationship" in tag_set:
            return score >= 0.94
        return score >= 0.93


@dataclass(slots=True)
class ReflectionResult:
    mode: str
    memory_ids: list[str] = field(default_factory=list)
    candidate_ids: list[str] = field(default_factory=list)
    skipped: int = 0
    segment_id: str | None = None
    error: str = ""


def reflection_prompt(*, user_name: str, assistant_name: str) -> tuple[str, list[str]]:
    system = (
        "You are the memory gateway's reflection step. Return JSON only, no explanation. "
        "From one turn of a close, casual, jumpy conversation, extract memory clues that are "
        "worth keeping for the long term. Do not moralise and do not flatten the tone; strong "
        "short lines are kept verbatim as quotes. A brief detour that comes back is not "
        "necessarily a topic change. "
        f"In this relationship the user is {user_name} and the assistant is {assistant_name}. "
        f"Always write {user_name} and {assistant_name}; never 'the user', 'the assistant' or 'the AI'. "
        "Write in the language the conversation is in."
    )
    rules = [
        "Only extract long-term facts, preferences, relationship settings, emotionally weighty "
        "lines, and recurring patterns. Ordinary small talk is not a memory.",
        "recent_context is what was said just before this turn. Use it only to resolve references "
        "('that', 'it', 'is it done yet'); do not extract its content again as if it were this turn.",
        "score is confidence that this is worth keeping long term, not emotional intensity. Anchors:",
        "  0.90 and above: identity, boundaries, promises, turning points, clear long-term facts. "
        "Most turns have none.",
        "  0.70 to 0.85: stable preferences, ongoing work, concrete plans, clear health or safety facts.",
        "  0.50 to 0.65: possibly useful but uncertain clues.",
        "  Ordinary affection, everyday flirting, general comfort, passing moods: below 0.50, or omit.",
        "Do not give every candidate a similar score. If nothing in this turn is worth keeping, "
        "return an empty memory_candidates array: that is correct and common.",
        "Choose kind honestly instead of filing everything under emotion: objective information is "
        "fact, likes and dislikes are preference, things done and technical progress are worklog, "
        "relationship settings and forms of address are relationship, concrete arrangements are "
        "plan. Use emotion only when the feeling itself is the content.",
        "importance is long-term weight from 1 to 10; only turning points, promises, boundaries "
        "and rare high-emotion lines get 7 or more.",
        "segment.summary must not read like a report. Avoid verbs like 'conducted', 'expressed', "
        "'confirmed', 'provided', 'engaged in' (in Chinese: 进行了、表达了、确认了、给予). Write who "
        "said what and who did what.",
        "Bad: 'The two engaged in a deep emotional exchange.' Good: 'She said the day was long; he "
        "said to come home and he would cook.'",
        "Strong short lines go into quotes exactly as written, not one word changed.",
        f"segment.do_not_surface may be true only when {user_name} explicitly asks not to keep or "
        "resurface something. Intimate or explicit content alone is never a reason to hide.",
    ]
    return system, rules


REFLECTION_SCHEMA = {
    "segment": {
        "title": "short topic title",
        "summary": "concise summary that keeps the emotional tone",
        "tags": ["topic tags"],
        "quotes": ["important original lines, verbatim"],
        "valence": "number -1..1",
        "arousal": "number 0..1",
        "do_not_surface": "boolean",
    },
    "memory_candidates": [
        {
            "kind": "relationship|preference|fact|quote|emotion|plan|worklog",
            "content": "candidate long-term memory, using the two names",
            "tags": ["tags"],
            "score": "number 0..1",
            "importance": "integer 1..10",
        }
    ],
}


class MemoryReflector:
    def __init__(
        self,
        *,
        database: Database,
        gateway: MemoryGateway,
        model: JsonModel | None,
        user_name: str = "User",
        assistant_name: str = "Assistant",
        policy: CandidatePolicy | None = None,
        do_not_surface_markers: tuple[str, ...] = DEFAULT_DO_NOT_SURFACE_MARKERS,
    ):
        self.database = database
        self.gateway = gateway
        self.model = model
        self.user_name = user_name
        self.assistant_name = assistant_name
        self.policy = policy or CandidatePolicy(
            filler_words=(user_name, assistant_name),
            party_names=(user_name, assistant_name),
        )
        self.markers = tuple(re.sub(r"\s+", "", marker.lower()) for marker in do_not_surface_markers)

    def do_not_surface_requested(self, text: str) -> bool:
        normalized = re.sub(r"\s+", "", (text or "").lower())
        return any(marker in normalized for marker in self.markers)

    async def reflect_turn(
        self,
        *,
        conversation_id: str,
        user_message: dict[str, Any],
        assistant_message: dict[str, Any],
    ) -> ReflectionResult:
        user_text = str(user_message.get("content") or "")
        assistant_text = str(assistant_message.get("content") or "")
        message_ids = [str(user_message.get("id") or ""), str(assistant_message.get("id") or "")]

        if self.do_not_surface_requested(user_text):
            self.database.mark_messages_do_not_surface(message_ids)
            return ReflectionResult(mode="do_not_surface")
        if self.model is None:
            return ReflectionResult(mode="disabled")

        system, rules = reflection_prompt(user_name=self.user_name, assistant_name=self.assistant_name)
        payload = {
            "user_name": self.user_name,
            "assistant_name": self.assistant_name,
            "recent_context": self._recent_context(conversation_id, exclude=set(message_ids)),
            "user_message": user_text,
            "assistant_reply": assistant_text,
            "schema": REFLECTION_SCHEMA,
            "rules": rules,
        }
        try:
            # 0.1 flattens "she said she missed him" into "expressed longing";
            # the schema is held by JSON mode, so a warmer temperature is safe.
            data = await self.model.complete_json(system=system, payload=payload, temperature=0.4, max_tokens=2000)
        except Exception as exc:
            # A timeout's str() is empty; without the type the failure is unreadable.
            return ReflectionResult(mode="model", error=f"{type(exc).__name__}: {str(exc)[:240]}")

        result = ReflectionResult(mode="model")
        segment = data.get("segment") if isinstance(data.get("segment"), dict) else {}
        affect = (
            optional_float_range(segment.get("valence"), -1.0, 1.0),
            optional_float_range(segment.get("arousal"), 0.0, 1.0),
        )
        self._store_candidates(data, conversation_id, message_ids, affect, result)
        if segment:
            self._store_segment(segment, conversation_id, message_ids, affect, result)
        return result

    def _recent_context(self, conversation_id: str, *, exclude: set[str]) -> list[dict[str, str]]:
        try:
            rows = self.database.list_messages(conversation_id, limit=RECENT_CONTEXT_TURNS * 2 + 4)
        except Exception:
            return []
        names = {"user": self.user_name, "assistant": self.assistant_name}
        context = [
            {"name": names.get(str(row["role"]), str(row["role"])), "text": trim(str(row["content"]), 300)}
            for row in rows
            if row["id"] not in exclude
            and row["role"] in names
            and not row.get("do_not_surface")
            and str(row["content"]).strip()
        ]
        return context[-(RECENT_CONTEXT_TURNS * 2):]

    def _store_candidates(
        self,
        data: dict[str, Any],
        conversation_id: str,
        message_ids: list[str],
        affect: tuple[float | None, float | None],
        result: ReflectionResult,
    ) -> None:
        raw = data.get("memory_candidates")
        per_kind: dict[str, int] = {}
        for item in (raw if isinstance(raw, list) else [])[:MAX_CANDIDATES_PER_TURN]:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content") or "").strip()[:1000]
            kind = (str(item.get("kind") or "note").strip().lower()[:50]) or "note"
            per_kind[kind] = per_kind.get(kind, 0) + 1
            if per_kind[kind] > CANDIDATE_PER_KIND_LIMIT.get(kind, 2):
                result.skipped += 1
                continue
            tags = clean_tags(item.get("tags"))
            score = float_range(item.get("score"), 0.6, 0.0, 1.0)
            importance = float_range(item.get("importance"), 6.0, 0.0, 10.0)
            if not content or self.policy.should_drop(content, score):
                result.skipped += 1
                continue
            if self._is_duplicate(content, kind):
                result.skipped += 1
                continue
            if self.policy.should_keep(content=content, kind=kind, tags=tags, score=score, importance=importance):
                # Memories born in this turn inherit its emotional reading, so
                # mood-aware recall has something to match against.
                row = self.gateway.remember(
                    content=content,
                    kind=kind,
                    importance=importance / 10.0,
                    valence=affect[0],
                    arousal=affect[1],
                )
                result.memory_ids.append(str(row["id"]))
            elif self.policy.should_queue(kind=kind, tags=tags, score=score):
                row = self.database.add_memory_candidate(
                    kind=kind,
                    content=content,
                    tags=tags,
                    score=score,
                    importance=importance / 10.0,
                    source="reflection",
                    conversation_id=conversation_id,
                    source_message_ids=[item for item in message_ids if item],
                )
                result.candidate_ids.append(str(row["id"]))
            else:
                result.skipped += 1

    def _is_duplicate(self, content: str, kind: str) -> bool:
        # Production compares embeddings first and falls back to this text
        # overlap; the reference kernel uses the deterministic part only.
        return any(
            text_similarity(content, existing) > DUPLICATE_SIMILARITY
            for existing in self.database.recent_texts_by_kind(kind)
        )

    def _store_segment(
        self,
        segment: dict[str, Any],
        conversation_id: str,
        message_ids: list[str],
        affect: tuple[float | None, float | None],
        result: ReflectionResult,
    ) -> None:
        summary = str(segment.get("summary") or "").strip()
        if not summary:
            return
        quotes = [quote for quote in (clean_quote(item, 120) for item in (segment.get("quotes") or [])[:5]) if quote]
        hidden = bool(segment.get("do_not_surface"))
        row = self.database.add_memory_segment(
            conversation_id=conversation_id,
            title=str(segment.get("title") or "").strip()[:120],
            summary=summary[:2000],
            quotes=quotes,
            tags=clean_tags(segment.get("tags")),
            valence=affect[0],
            arousal=affect[1],
            start_message_id=message_ids[0] or None,
            end_message_id=message_ids[1] or None,
            do_not_surface=hidden,
        )
        if hidden:
            self.database.mark_messages_do_not_surface(message_ids)
        result.segment_id = str(row.get("id") or "") or None

