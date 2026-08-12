from __future__ import annotations


def create_room(client) -> str:
    response = client.post(
        "/api/rooms",
        json={
            "title": "Review room",
            "participants": [
                {
                    "agent_id": "claude",
                    "display_name": "Claude",
                    "provider_id": "echo",
                    "position": 1,
                    "role_prompt": "Propose the first solution.",
                },
                {
                    "agent_id": "codex",
                    "display_name": "Codex",
                    "provider_id": "echo",
                    "position": 2,
                    "role_prompt": "Review and complete the first solution.",
                },
            ],
        },
    )
    assert response.status_code == 200
    return response.json()["id"]


def test_room_uses_fixed_order_and_replays_duplicate(client, echo_provider):
    room_id = create_room(client)
    payload = {
        "request_id": "room-request-0001",
        "content": "Design an idempotent retry flow.",
    }

    first = client.post(f"/api/rooms/{room_id}/turns", json=payload)
    second = client.post(f"/api/rooms/{room_id}/turns", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert [message["author_id"] for message in first.json()["messages"]] == [
        "human",
        "claude",
        "codex",
    ]
    assert echo_provider.calls == 2


def test_room_rejects_duplicate_positions(client):
    response = client.post(
        "/api/rooms",
        json={
            "title": "Invalid",
            "participants": [
                {
                    "agent_id": "one",
                    "display_name": "One",
                    "provider_id": "echo",
                    "position": 1,
                },
                {
                    "agent_id": "two",
                    "display_name": "Two",
                    "provider_id": "echo",
                    "position": 1,
                },
            ],
        },
    )
    assert response.status_code == 400
