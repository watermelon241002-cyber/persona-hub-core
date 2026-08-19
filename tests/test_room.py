from __future__ import annotations

from persona_hub.providers import GenerationRequest, GenerationResult


class RecordingProvider:
    provider_id = "recording"

    def __init__(self) -> None:
        self.requests: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.requests.append(request)
        return GenerationResult(text="ok", model_id="recording-v1")


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


def test_room_turn_same_request_id_different_content_conflicts(client):
    room_id = create_room(client)
    first = client.post(
        f"/api/rooms/{room_id}/turns",
        json={"request_id": "room-conflict-0001", "content": "Original prompt."},
    )
    changed = client.post(
        f"/api/rooms/{room_id}/turns",
        json={"request_id": "room-conflict-0001", "content": "Different prompt."},
    )

    assert first.status_code == 200
    assert changed.status_code == 409


def test_role_prompt_reaches_provider_system_prompt(app_bundle):
    app, _, providers = app_bundle
    recording = RecordingProvider()
    providers.register(recording)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        response = client.post(
            "/api/rooms",
            json={
                "title": "Prompt room",
                "participants": [
                    {
                        "agent_id": "claude",
                        "display_name": "Claude",
                        "provider_id": "recording",
                        "position": 1,
                        "role_prompt": "Propose the first implementation.",
                    }
                ],
            },
        )
        assert response.status_code == 200
        room_id = response.json()["id"]
        turn = client.post(
            f"/api/rooms/{room_id}/turns",
            json={"request_id": "room-prompt-0001", "content": "Start."},
        )

    assert turn.status_code == 200
    assert len(recording.requests) == 1
    system_prompt = recording.requests[0].system_prompt
    assert "Propose the first implementation." in system_prompt
    assert system_prompt.startswith("You are the same careful persona")
