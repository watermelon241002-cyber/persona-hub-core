"""Memory gateway: recall that happens *before* the model answers.

Two things here are deliberately different from a plain vector search, and both
were learned the hard way in a long-running deployment.

**1. Some memories must not compete for ranking.**
Identity, safety facts and standing boundaries are not "relevant to the current
topic" -- they are relevant to every topic. If they are ranked like everything
else, they lose to whatever the user happens to be talking about, and the model
answers without knowing who it is talking to. Pinned rows enter a reserved slot
and skip scoring entirely.

**2. Similarity alone is the wrong ranking function.**
A pure embedding search keeps returning the same high-similarity rows and
silently drops memories that matter for other reasons: they are recent, they are
important, or they match the emotional register of the moment. Worse, a fixed
per-source score (a common shortcut) makes old high-scoring rows reappear
forever while genuinely relevant new ones never get in. `recall()` blends seven
signals instead; the weights are data, not hard-coded arithmetic.

Note on the emotional axis: an embedder cannot supply it. "I am so happy" and
"I am so sad" are near-neighbours in embedding space -- same shape, same
register, opposite feeling. Recall that should follow mood needs valence and
arousal stored as their own dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Mapping

from .db import Database


TOKEN_PATTERN = re.compile(r"[\w㐀-鿿]+", re.UNICODE)

# Maximum distance in the (valence, arousal) plane: valence spans -1..1 and
# arousal spans 0..1, so the diagonal is sqrt(2^2 + 1^2).
MAX_EMOTION_DISTANCE = math.sqrt(5.0)

# How much prior weight a memory earns from its kind alone. Identity, safety and
# boundary rows outrank episodic chatter when every other signal ties.
KIND_PRIORS: Mapping[str, float] = {
    "identity": 1.00,
    "boundary": 0.95,
    "safety": 0.95,
    "relationship": 0.80,
    "preference": 0.75,
    "fact": 0.70,
    "plan": 0.65,
    "project": 0.65,
    "episodic": 0.55,
    "note": 0.50,
}
DEFAULT_KIND_PRIOR = 0.55

# Kinds that describe *who the parties are* rather than what happened.
IDENTITY_KINDS = frozenset({"identity", "boundary", "safety", "relationship"})


@dataclass(frozen=True, slots=True)
class RecallWeights:
    """Blend weights for :meth:`MemoryGateway.recall`.

    Defaults follow the reference design. They must sum to 1.0 so a final score
    stays inside 0..1 and remains comparable across queries.
    """

    semantic: float = 0.40
    source_prior: float = 0.18
    recency: float = 0.14
    importance: float = 0.12
    emotion: float = 0.08
    identity: float = 0.05
    jitter: float = 0.03

    def total(self) -> float:
        return (
            self.semantic
            + self.source_prior
            + self.recency
            + self.importance
            + self.emotion
            + self.identity
            + self.jitter
        )

    def __post_init__(self) -> None:
        if abs(self.total() - 1.0) > 1e-6:
            raise ValueError(f"Recall weights must sum to 1.0, got {self.total()}")


class LocalHashEmbedder:
    """Small deterministic demo embedder with no external model dependency.

    It is useful for tests and local evaluation, not a replacement for a
    production multilingual embedding model.
    """

    def __init__(self, dimension: int = 128):
        if dimension < 16:
            raise ValueError("Embedding dimension must be at least 16")
        self.dimension = dimension

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        normalized = text.casefold().strip()
        tokens = TOKEN_PATTERN.findall(normalized)
        features = tokens + [normalized[index : index + 2] for index in range(max(0, len(normalized) - 1))]
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return vector


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        return 0.0
    return sum(a * b for a, b in zip(left, right))


def emotion_similarity(
    left: tuple[float | None, float | None],
    right: tuple[float | None, float | None],
) -> float:
    """Closeness of two points in the (valence, arousal) plane, in 0..1.

    Returns a neutral 0.5 when either side is unknown, so a memory without an
    emotional reading is neither rewarded nor punished for it.
    """

    left_valence, left_arousal = left
    right_valence, right_arousal = right
    if None in (left_valence, left_arousal, right_valence, right_arousal):
        return 0.5
    distance = math.hypot(
        float(left_valence) - float(right_valence),
        float(left_arousal) - float(right_arousal),
    )
    return max(0.0, 1.0 - distance / MAX_EMOTION_DISTANCE)


def recency_score(created_at: str | None, *, now: datetime, half_life_days: float = 30.0) -> float:
    """Exponential decay in 0..1. Old memories fade, they are never deleted."""

    moment = _parse_timestamp(created_at)
    if moment is None:
        return 0.5
    age_days = max(0.0, (now - moment).total_seconds() / 86_400.0)
    return math.pow(0.5, age_days / max(half_life_days, 0.5))


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _stable_jitter(memory_id: str, query: str) -> float:
    """Deterministic pseudo-randomness in 0..1.

    Recall needs a little non-determinism, otherwise the same handful of rows
    surface forever and the whole thing feels mechanical. Deriving it from
    (id, query) keeps that variety while leaving results reproducible -- a real
    random source would make recall untestable.
    """

    digest = hashlib.blake2b(f"{memory_id}\x1f{query}".encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "big") / 0xFFFF_FFFF


def _field(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    id: str
    kind: str
    content: str
    importance: float
    score: float
    occurred_at: str | None
    created_at: str
    pinned: bool = False
    valence: float | None = None
    arousal: float | None = None
    quotes: list[str] = field(default_factory=list)
    scene: str = ""
    signals: dict[str, float] = field(default_factory=dict)


class MemoryGateway:
    def __init__(
        self,
        database: Database,
        embedder: LocalHashEmbedder | None = None,
        weights: RecallWeights | None = None,
    ):
        self.database = database
        self.embedder = embedder or LocalHashEmbedder()
        self.weights = weights or RecallWeights()

    def remember(
        self,
        *,
        content: str,
        kind: str = "episodic",
        importance: float = 0.5,
        occurred_at: str | None = None,
        pinned: bool = False,
        valence: float | None = None,
        arousal: float | None = None,
        quotes: list[str] | None = None,
        scene: str = "",
    ) -> dict:
        embedding = self.embedder.embed(content)
        return self.database.add_memory(
            kind=kind,
            content=content,
            importance=importance,
            embedding=embedding,
            occurred_at=occurred_at,
            pinned=pinned,
            valence=valence,
            arousal=arousal,
            quotes=quotes,
            scene=scene,
        )

    def recall(
        self,
        query: str,
        limit: int = 5,
        *,
        mood: tuple[float | None, float | None] = (None, None),
        now: datetime | None = None,
    ) -> list[RecalledMemory]:
        """Return pinned memories first, then the best-blended remainder.

        `mood` is the emotional reading of the current moment. Supplying it lets
        recall lean toward memories that felt the same way, which is the one
        association a semantic index cannot make.
        """

        limit = max(0, min(limit, 20))
        if not limit:
            return []
        moment = now or datetime.now(timezone.utc)
        query_vector = self.embedder.embed(query)

        reserved: list[RecalledMemory] = []
        ranked: list[RecalledMemory] = []
        for row in self.database.list_memories():
            if bool(_field(row, "pinned", 0)):
                # Reserved slot: no scoring, no per-kind quota, no competition.
                reserved.append(self._build(row, score=1.0, signals={"pinned": 1.0}))
                continue
            ranked.append(self._score(row, query=query, query_vector=query_vector, mood=mood, now=moment))

        # Pinned rows must never crowd out the entire result. Half the budget is
        # the ceiling; identity that drowns the actual conversation is its own
        # kind of failure.
        reserve_cap = max(1, limit // 2)
        reserved.sort(key=lambda item: (item.importance, item.created_at), reverse=True)
        reserved = reserved[:reserve_cap]

        ranked.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        remaining = limit - len(reserved)
        return [*reserved, *ranked[:remaining]] if remaining > 0 else reserved

    def _score(
        self,
        row: Mapping[str, Any],
        *,
        query: str,
        query_vector: list[float],
        mood: tuple[float | None, float | None],
        now: datetime,
    ) -> RecalledMemory:
        weights = self.weights
        memory_vector = json.loads(_field(row, "embedding_json", "[]"))
        kind = str(_field(row, "kind", "episodic"))
        valence = _field(row, "valence")
        arousal = _field(row, "arousal")

        signals = {
            "semantic": max(0.0, cosine_similarity(query_vector, memory_vector)),
            "source_prior": KIND_PRIORS.get(kind, DEFAULT_KIND_PRIOR),
            "recency": recency_score(_field(row, "created_at"), now=now),
            "importance": max(0.0, min(1.0, float(_field(row, "importance", 0.5)))),
            "emotion": emotion_similarity(
                (None if valence is None else float(valence), None if arousal is None else float(arousal)),
                mood,
            ),
            "identity": 1.0 if kind in IDENTITY_KINDS else 0.0,
            "jitter": _stable_jitter(str(_field(row, "id", "")), query),
        }
        score = (
            weights.semantic * signals["semantic"]
            + weights.source_prior * signals["source_prior"]
            + weights.recency * signals["recency"]
            + weights.importance * signals["importance"]
            + weights.emotion * signals["emotion"]
            + weights.identity * signals["identity"]
            + weights.jitter * signals["jitter"]
        )
        return self._build(row, score=max(0.0, min(1.0, score)), signals=signals)

    def _build(self, row: Mapping[str, Any], *, score: float, signals: dict[str, float]) -> RecalledMemory:
        quotes_raw = _field(row, "quotes_json", "[]")
        try:
            quotes = json.loads(quotes_raw) if isinstance(quotes_raw, str) else list(quotes_raw or [])
        except json.JSONDecodeError:
            quotes = []
        valence = _field(row, "valence")
        arousal = _field(row, "arousal")
        return RecalledMemory(
            id=str(row["id"]),
            kind=str(_field(row, "kind", "episodic")),
            content=str(_field(row, "content", "")),
            importance=float(_field(row, "importance", 0.5)),
            score=score,
            occurred_at=_field(row, "occurred_at"),
            created_at=str(_field(row, "created_at", "")),
            pinned=bool(_field(row, "pinned", 0)),
            valence=None if valence is None else float(valence),
            arousal=None if arousal is None else float(arousal),
            quotes=[str(item) for item in quotes if str(item).strip()],
            scene=str(_field(row, "scene", "")),
            signals=signals,
        )
