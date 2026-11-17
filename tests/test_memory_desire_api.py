from __future__ import annotations

import time

from fastapi.testclient import TestClient

from persona_hub.app import create_app
from persona_hub.config import Settings
from persona_hub.db import Database
from persona_hub.providers import EchoProvider, ProviderRegistry


def chat(client: TestClient, content: str, request_id: str) -> dict:
    conversation = client.post("/api/conversations", json={"title": "t"}).json()
    response = client.post(
        "/api/chat",
        json={"conversation_id": conversation["id"], "request_id": request_id, "content": content},
    )
    assert response.status_code == 200
    return response.json()


def test_desire_state_and_user_message_pulse(client):
    before = client.get("/api/desire/state").json()
    assert before["gates"]["driven"] is False
    assert before["heartbeat"]["running"] is False
    chat(client, "hello there", "desire-pulse-1")
    after = client.get("/api/desire/state").json()
    assert after["drive"]["attachment"] > before["drive"]["attachment"]
    assert client.get("/api/desire/activity").json() == []


def test_do_not_surface_request_is_honoured_in_the_background(client, app_bundle):
    _, database, _ = app_bundle
    body = chat(client, "这句别存，谢谢", "dns-request-1")
    message_id = body["user_message"]["id"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and not database.get_message(message_id)["do_not_surface"]:
        time.sleep(0.02)
    assert database.get_message(message_id)["do_not_surface"] == 1


def test_candidate_review_flow(client, app_bundle):
    _, database, _ = app_bundle
    candidate = database.add_memory_candidate(
        kind="quote",
        content="user: finally a window that faces east",
        tags=["quote"],
        score=0.96,
        importance=0.6,
        source="reflection",
        conversation_id=None,
        source_message_ids=[],
    )
    pending = client.get("/api/memory/candidates").json()
    assert [item["id"] for item in pending] == [candidate["id"]]
    assert pending[0]["tags"] == ["quote"]

    accepted = client.post(f"/api/memory/candidates/{candidate['id']}/accept")
    assert accepted.status_code == 200
    assert accepted.json()["kind"] == "quote"
    assert client.post(f"/api/memory/candidates/{candidate['id']}/accept").status_code == 409
    assert client.post(f"/api/memory/candidates/{candidate['id']}/reject").status_code == 409
    assert client.post("/api/memory/candidates/missing/reject").status_code == 404
    assert client.get("/api/memory/candidates").json() == []


def test_summaries_endpoint_requires_a_model(client):
    response = client.post("/api/memory/summaries/run", json={"summary_type": "daily"})
    assert response.status_code == 409
    assert client.get("/api/memory/summaries").json() == []


def test_summaries_endpoint_runs_with_a_model(tmp_path):
    class Model:
        async def complete_json(self, **kwargs):
            return {"summary": "a quiet day", "quotes": [], "scene": "", "valence": 0, "arousal": 0, "tags": []}

    settings = Settings(
        database_path=tmp_path / "s.sqlite3",
        persona_file=tmp_path / "missing.md",
        desire_state_path=tmp_path / "desire.json",
    )
    providers = ProviderRegistry()
    providers.register(EchoProvider())
    app = create_app(settings, database=Database(settings.database_path), providers=providers, memory_model=Model())
    with TestClient(app) as client:
        conversation = client.post("/api/conversations", json={}).json()
        app.state.database.append_message(
            conversation_id=conversation["id"], role="user", content="hi", created_at="2026-01-05T10:00:00+00:00"
        )
        response = client.post(
            "/api/memory/summaries/run",
            json={"summary_type": "daily", "start": "2026-01-05T00:00:00Z", "end": "2026-01-06T00:00:00Z"},
        )
        assert response.status_code == 200
        assert response.json()["created"] == 1
        assert client.get("/api/memory/summaries").json()[0]["content"] == "a quiet day"
