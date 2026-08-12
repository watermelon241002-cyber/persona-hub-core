from __future__ import annotations


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
