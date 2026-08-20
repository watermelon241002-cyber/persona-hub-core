from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from persona_hub.db import Database
from persona_hub.memory import (
    MemoryGateway,
    RecallWeights,
    emotion_similarity,
    recency_score,
)


@pytest.fixture
def gateway(tmp_path: Path) -> MemoryGateway:
    database = Database(tmp_path / "memory.sqlite3")
    database.initialize()
    return MemoryGateway(database)


def test_memory_can_be_stored_and_recalled(client):
    green = client.post(
        "/api/memories",
        json={
            "content": "The lighthouse project uses a green status color.",
            "kind": "project",
            "importance": 0.8,
        },
    )
    blue = client.post(
        "/api/memories",
        json={
            "content": "The unrelated notebook has a blue cover.",
            "kind": "object",
            "importance": 0.4,
        },
    )
    assert green.status_code == 200
    assert blue.status_code == 200
    assert "embedding_json" not in green.json()

    response = client.get(
        "/api/memories/recall",
        params={"q": "What color does the lighthouse project use?", "limit": 2},
    )
    assert response.status_code == 200
    memories = response.json()
    assert len(memories) == 2
    assert {item["id"] for item in memories} == {green.json()["id"], blue.json()["id"]}
    assert all(0.0 <= item["score"] <= 1.0 for item in memories)


def test_recalled_memory_is_added_to_chat_context(client):
    memory = client.post(
        "/api/memories",
        json={"content": "The demo launch word is sunrise.", "importance": 1.0},
    ).json()
    conversation_id = client.post("/api/conversations", json={}).json()["id"]
    response = client.post(
        "/api/chat",
        json={
            "conversation_id": conversation_id,
            "request_id": "memory-chat-0001",
            "content": "What is the demo launch word?",
            "provider_id": "echo",
        },
    )
    assert response.status_code == 200
    assert memory["id"] in response.json()["recalled_memory_ids"]


def test_pinned_memory_surfaces_for_an_unrelated_query(gateway: MemoryGateway):
    """The failure this prevents: identity losing a ranking contest.

    A pinned row shares no vocabulary with the query below. Ranked normally it
    would never place, and the model would answer without knowing it.
    """

    pinned = gateway.remember(
        content="The operator prefers to be addressed by their full name.",
        kind="identity",
        importance=1.0,
        pinned=True,
    )
    for index in range(8):
        gateway.remember(
            content=f"Sprint note {index}: the deployment pipeline was rebuilt today.",
            kind="episodic",
            importance=0.9,
        )

    recalled = gateway.recall("what should we have for lunch today", limit=4)

    assert recalled[0].id == pinned["id"]
    assert recalled[0].pinned is True
    assert recalled[0].signals == {"pinned": 1.0}


def test_pinned_memories_cannot_take_over_the_whole_budget(gateway: MemoryGateway):
    """Identity that drowns the conversation is its own kind of failure."""

    for index in range(6):
        gateway.remember(content=f"Standing rule {index}.", kind="boundary", pinned=True)
    for index in range(6):
        gateway.remember(content=f"Ordinary note {index}.", kind="episodic")

    recalled = gateway.recall("ordinary note", limit=6)

    assert len(recalled) == 6
    assert sum(1 for item in recalled if item.pinned) == 3
    assert any(not item.pinned for item in recalled)


def test_recall_blends_signals_instead_of_similarity_alone(gateway: MemoryGateway):
    """A fixed per-source score makes stale rows reappear forever.

    Both memories below are equally unrelated to the query, so the semantic term
    ties. The blend must then be decided by the remaining signals.
    """

    weak = gateway.remember(content="Archived trivia about paper clips.", kind="note", importance=0.1)
    strong = gateway.remember(
        content="Archived trivia about staplers.", kind="safety", importance=1.0
    )

    recalled = gateway.recall("completely unrelated subject", limit=2)
    ranked = {item.id: item.score for item in recalled}

    assert ranked[strong["id"]] > ranked[weak["id"]]
    signals = next(item.signals for item in recalled if item.id == strong["id"])
    assert signals["semantic"] == pytest.approx(
        next(item.signals["semantic"] for item in recalled if item.id == weak["id"]),
        abs=0.05,
    )
    assert signals["importance"] == pytest.approx(1.0)
    assert signals["source_prior"] > 0.9
    assert signals["identity"] == pytest.approx(1.0)  # safety counts as identity


def test_recall_can_follow_the_mood_of_the_moment(gateway: MemoryGateway):
    """The one association an embedding cannot make.

    Both rows are worded almost identically, so semantic similarity cannot
    separate them. Only the stored affect can.
    """

    upbeat = gateway.remember(
        content="A quiet evening that felt calm and steady.",
        kind="episodic",
        valence=0.9,
        arousal=0.2,
    )
    bleak = gateway.remember(
        content="A quiet evening that felt heavy and tense.",
        kind="episodic",
        valence=-0.9,
        arousal=0.8,
    )

    low_mood = gateway.recall("a quiet evening", limit=2, mood=(-0.8, 0.7))
    high_mood = gateway.recall("a quiet evening", limit=2, mood=(0.8, 0.2))

    assert low_mood[0].id == bleak["id"]
    assert high_mood[0].id == upbeat["id"]


def test_affect_fields_survive_the_round_trip(client):
    created = client.post(
        "/api/memories",
        json={
            "content": "The team agreed to keep Friday releases frozen.",
            "kind": "boundary",
            "importance": 0.9,
            "pinned": True,
            "valence": -0.2,
            "arousal": 0.6,
            "quotes": ["Let's not ship on a Friday again."],
            "scene": "Late retro, everyone tired, decision made quickly.",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["pinned"] is True
    assert body["quotes"] == ["Let's not ship on a Friday again."]
    assert body["scene"].startswith("Late retro")
    assert "quotes_json" not in body

    recalled = client.get(
        "/api/memories/recall", params={"q": "anything at all", "limit": 3}
    ).json()
    assert recalled[0]["pinned"] is True
    assert recalled[0]["quotes"] == ["Let's not ship on a Friday again."]


def test_emotion_similarity_is_neutral_when_unknown():
    assert emotion_similarity((None, None), (0.5, 0.5)) == pytest.approx(0.5)
    assert emotion_similarity((0.5, 0.5), (None, 0.5)) == pytest.approx(0.5)
    assert emotion_similarity((0.4, 0.3), (0.4, 0.3)) == pytest.approx(1.0)
    assert emotion_similarity((1.0, 1.0), (-1.0, 0.0)) == pytest.approx(0.0)


def test_recency_decays_but_never_reaches_zero():
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)
    fresh = recency_score(now.isoformat(), now=now)
    month_old = recency_score((now - timedelta(days=30)).isoformat(), now=now)
    year_old = recency_score((now - timedelta(days=365)).isoformat(), now=now)

    assert fresh == pytest.approx(1.0)
    assert month_old == pytest.approx(0.5, abs=0.01)
    assert 0.0 < year_old < month_old
    assert recency_score(None, now=now) == pytest.approx(0.5)


def test_recall_weights_must_sum_to_one():
    RecallWeights()  # defaults are valid
    with pytest.raises(ValueError, match="must sum to 1.0"):
        RecallWeights(semantic=0.9)


def test_recall_is_reproducible_for_the_same_query(gateway: MemoryGateway):
    """Jitter keeps recall from feeling mechanical without making it untestable."""

    for index in range(5):
        gateway.remember(content=f"Interchangeable note {index}.", kind="note")

    first = [item.id for item in gateway.recall("note", limit=5)]
    second = [item.id for item in gateway.recall("note", limit=5)]

    assert first == second
