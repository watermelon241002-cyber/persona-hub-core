from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re

from .db import Database


TOKEN_PATTERN = re.compile(r"[\w\u3400-\u9fff]+", re.UNICODE)


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


@dataclass(frozen=True, slots=True)
class RecalledMemory:
    id: str
    kind: str
    content: str
    importance: float
    score: float
    occurred_at: str | None
    created_at: str


class MemoryGateway:
    def __init__(self, database: Database, embedder: LocalHashEmbedder | None = None):
        self.database = database
        self.embedder = embedder or LocalHashEmbedder()

    def remember(
        self,
        *,
        content: str,
        kind: str = "episodic",
        importance: float = 0.5,
        occurred_at: str | None = None,
    ) -> dict:
        embedding = self.embedder.embed(content)
        return self.database.add_memory(
            kind=kind,
            content=content,
            importance=importance,
            embedding=embedding,
            occurred_at=occurred_at,
        )

    def recall(self, query: str, limit: int = 5) -> list[RecalledMemory]:
        query_vector = self.embedder.embed(query)
        ranked: list[RecalledMemory] = []
        for row in self.database.list_memories():
            memory_vector = json.loads(row["embedding_json"])
            semantic = max(0.0, cosine_similarity(query_vector, memory_vector))
            score = 0.8 * semantic + 0.2 * float(row["importance"])
            ranked.append(
                RecalledMemory(
                    id=row["id"],
                    kind=row["kind"],
                    content=row["content"],
                    importance=float(row["importance"]),
                    score=score,
                    occurred_at=row["occurred_at"],
                    created_at=row["created_at"],
                )
            )
        ranked.sort(key=lambda item: (item.score, item.created_at), reverse=True)
        return ranked[: max(0, min(limit, 20))]
