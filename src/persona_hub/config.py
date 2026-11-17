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
    # The people's own time zone: summary days, quiet hours and "today".
    timezone: str = "UTC"
    user_display_name: str = "User"
    assistant_display_name: str = "Assistant"
    # Background memory work uses the OpenAI-compatible endpoint above with
    # its own (usually cheaper) model. Empty disables model-backed memory.
    memory_model: str = ""
    memory_reflection: bool = False
    memory_summaries: bool = False
    # Desire heartbeat. Behaviour is additionally gated by the
    # PERSONA_HUB_DESIRE_* flags, which all default to off.
    desire_enabled: bool = False
    desire_state_path: Path = Path("data/desire_state.json")
    desire_quiet_hours: str = "02-08"

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
            timezone=os.getenv("PERSONA_HUB_TIMEZONE", "UTC"),
            user_display_name=os.getenv("PERSONA_HUB_USER_NAME", "User"),
            assistant_display_name=os.getenv("PERSONA_HUB_ASSISTANT_NAME", "Assistant"),
            memory_model=os.getenv("PERSONA_HUB_MEMORY_MODEL", ""),
            memory_reflection=_env_flag("PERSONA_HUB_MEMORY_REFLECTION"),
            memory_summaries=_env_flag("PERSONA_HUB_MEMORY_SUMMARIES"),
            desire_enabled=_env_flag("PERSONA_HUB_DESIRE"),
            desire_state_path=Path(
                os.getenv("PERSONA_HUB_DESIRE_STATE", "data/desire_state.json")
            ),
            desire_quiet_hours=os.getenv("PERSONA_HUB_DESIRE_QUIET_HOURS", "02-08"),
        )

    def quiet_hours(self) -> tuple[int, int]:
        """Parse "02-08" into (2, 8). A window may wrap midnight ("23-07")."""

        try:
            start_text, end_text = self.desire_quiet_hours.split("-", 1)
            start, end = int(start_text), int(end_text)
        except ValueError as exc:
            raise ValueError(
                f"PERSONA_HUB_DESIRE_QUIET_HOURS must look like 02-08, got {self.desire_quiet_hours!r}"
            ) from exc
        if not (0 <= start <= 23 and 0 <= end <= 24):
            raise ValueError("Quiet hours must be within 0-24")
        return start, end

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
