from __future__ import annotations

from persona_hub.providers import GenerationRequest, GenerationResult, ProviderRegistry


def create_conversation(client) -> str:
    response = client.post("/api/conversations", json={"title": "Test"})
    assert response.status_code == 200
    return response.json()["id"]


def test_chat_request_is_idempotent(client, echo_provider):
    conversation_id = create_conversation(client)
    payload = {
        "conversation_id": conversation_id,
        "request_id": "chat-request-0001",
        "content": "Hello from one logical request.",
        "provider_id": "echo",
    }

    first = client.post("/api/chat", json=payload)
    second = client.post("/api/chat", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["replayed"] is False
    assert second.json()["replayed"] is True
    assert first.json()["assistant_message"]["id"] == second.json()["assistant_message"]["id"]
    assert echo_provider.calls == 1

    messages = client.get(
        f"/api/conversations/{conversation_id}/messages"
    ).json()
    assert [message["role"] for message in messages] == ["user", "assistant"]


def test_same_request_id_with_different_payload_conflicts(client, echo_provider):
    conversation_id = create_conversation(client)
    payload = {
        "conversation_id": conversation_id,
        "request_id": "conflict-request-0001",
        "content": "Original payload.",
        "provider_id": "echo",
    }

    first = client.post("/api/chat", json=payload)
    changed = client.post(
        "/api/chat", json={**payload, "content": "Tampered payload."}
    )

    assert first.status_code == 200
    assert changed.status_code == 409
    assert echo_provider.calls == 1


class FailingProvider:
    provider_id = "failing"

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        raise RuntimeError("synthetic upstream failure")


def test_failed_request_requires_new_request_id(app_bundle):
    app, _, providers = app_bundle
    failing = FailingProvider()
    providers.register(failing)

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        conversation_id = create_conversation(client)
        payload = {
            "conversation_id": conversation_id,
            "request_id": "failed-request-0001",
            "content": "Fail once, never duplicate silently.",
            "provider_id": "failing",
        }
        first = client.post("/api/chat", json=payload)
        second = client.post("/api/chat", json=payload)

    assert first.status_code == 502
    assert second.status_code == 409
    assert failing.calls == 1
