from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from persona_hub.app import create_app
from persona_hub.config import Settings
from persona_hub.db import Database
from persona_hub.providers import EchoProvider, ProviderRegistry


@pytest.fixture
def echo_provider() -> EchoProvider:
    return EchoProvider()


@pytest.fixture
def app_bundle(tmp_path: Path, echo_provider: EchoProvider):
    persona_file = tmp_path / "persona.md"
    persona_file.write_text(
        "You are the same careful persona across every runtime.", encoding="utf-8"
    )
    settings = Settings(
        database_path=tmp_path / "test.sqlite3",
        persona_file=persona_file,
        worker_token="test-worker-token",
        default_provider="echo",
    )
    database = Database(settings.database_path)
    providers = ProviderRegistry()
    providers.register(echo_provider)
    app = create_app(settings, database=database, providers=providers)
    return app, database, providers


@pytest.fixture
def client(app_bundle):
    app, _, _ = app_bundle
    with TestClient(app) as test_client:
        yield test_client
