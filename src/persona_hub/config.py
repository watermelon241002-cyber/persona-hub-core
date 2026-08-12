from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 18080
    database_path: Path = Path("data/persona_hub.sqlite3")
    persona_file: Path = Path("prompts/persona.example.md")
    worker_token: str = "development-worker-token"
    default_provider: str = "echo"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            environment=os.getenv("PERSONA_HUB_ENV", "development"),
            host=os.getenv("PERSONA_HUB_HOST", "127.0.0.1"),
            port=int(os.getenv("PERSONA_HUB_PORT", "18080")),
            database_path=Path(
                os.getenv("PERSONA_HUB_DATABASE", "data/persona_hub.sqlite3")
            ),
            persona_file=Path(
                os.getenv("PERSONA_HUB_PERSONA_FILE", "prompts/persona.example.md")
            ),
            worker_token=os.getenv(
                "PERSONA_HUB_WORKER_TOKEN", "development-worker-token"
            ),
            default_provider=os.getenv("PERSONA_HUB_DEFAULT_PROVIDER", "echo"),
            openai_base_url=os.getenv("OPENAI_COMPATIBLE_BASE_URL", ""),
            openai_api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY", ""),
            openai_model=os.getenv("OPENAI_COMPATIBLE_MODEL", ""),
        )

    def validate_for_production(self) -> None:
        if self.environment != "production":
            return
        if self.worker_token == "development-worker-token":
            raise ValueError("Set a strong PERSONA_HUB_WORKER_TOKEN in production")
        if self.host not in {"127.0.0.1", "::1"}:
            raise ValueError("Bind production behind a reverse proxy on loopback")
