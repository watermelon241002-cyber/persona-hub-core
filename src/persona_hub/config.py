from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class Settings:
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 18080
    database_path: Path = Path("data/persona_hub.sqlite3")
    persona_file: Path = Path("prompts/persona.example.md")
    worker_token: str = "development-worker-token"
    api_token: str = ""
    allow_public_bind: bool = False
    default_provider: str = "echo"
    openai_base_url: str = ""
    openai_api_key: str = ""
    openai_model: str = ""

    @classmethod
    def from_env(cls) -> "Settings":
        # Real environment variables always win; .env only fills the gaps.
        load_dotenv(override=False)
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
            api_token=os.getenv("PERSONA_HUB_API_TOKEN", ""),
            allow_public_bind=_env_flag("PERSONA_HUB_ALLOW_PUBLIC_BIND"),
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
        if not self.api_token:
            raise ValueError(
                "Set PERSONA_HUB_API_TOKEN in production so the REST API requires"
                " authentication"
            )
        if self.host not in {"127.0.0.1", "::1"} and not self.allow_public_bind:
            raise ValueError(
                "Bind production behind a reverse proxy on loopback, or set"
                " PERSONA_HUB_ALLOW_PUBLIC_BIND=1 when a wider bind is safe"
                " (for example inside a container whose published port stays on"
                " the host loopback)"
            )
