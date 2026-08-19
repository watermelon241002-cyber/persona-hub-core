from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persona_hub.app import create_app
from persona_hub.config import Settings
from persona_hub.db import Database
from persona_hub.providers import EchoProvider, ProviderRegistry


def build_client(tmp_path: Path, api_token: str) -> TestClient:
    persona_file = tmp_path / "persona.md"
    persona_file.write_text("Same persona across runtimes.", encoding="utf-8")
    settings = Settings(
        database_path=tmp_path / "auth-test.sqlite3",
        persona_file=persona_file,
        worker_token="test-worker-token",
        api_token=api_token,
    )
    database = Database(settings.database_path)
    providers = ProviderRegistry()
    providers.register(EchoProvider())
    return TestClient(create_app(settings, database=database, providers=providers))


def test_api_requires_bearer_token_when_configured(tmp_path):
    with build_client(tmp_path, api_token="secret-token") as client:
        assert client.get("/api/providers").status_code == 401
        wrong = client.get(
            "/api/providers", headers={"Authorization": "Bearer wrong"}
        )
        assert wrong.status_code == 401
        ok = client.get(
            "/api/providers", headers={"Authorization": "Bearer secret-token"}
        )
        assert ok.status_code == 200
        # Health probes stay reachable for load balancers and containers.
        assert client.get("/health/live").status_code == 200


def test_api_open_when_token_is_empty(tmp_path):
    with build_client(tmp_path, api_token="") as client:
        assert client.get("/api/providers").status_code == 200


def test_production_requires_api_token(tmp_path):
    settings = Settings(
        environment="production",
        worker_token="a-strong-token",
        api_token="",
        database_path=tmp_path / "prod.sqlite3",
    )
    with pytest.raises(ValueError, match="PERSONA_HUB_API_TOKEN"):
        settings.validate_for_production()


def test_production_allows_public_bind_only_with_flag():
    base = dict(
        environment="production",
        worker_token="a-strong-token",
        api_token="also-strong",
    )
    with pytest.raises(ValueError, match="ALLOW_PUBLIC_BIND"):
        Settings(host="0.0.0.0", **base).validate_for_production()
    Settings(host="0.0.0.0", allow_public_bind=True, **base).validate_for_production()
