"""Runtime settings. Secrets come from the environment, never from the repo."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader. Does not override variables that are already set."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Settings:
    xai_api_key: str | None
    live_voice: bool
    live_data: bool
    tool_secret: str | None
    db_path: Path
    data_dir: Path
    bind: str
    port: int
    cors_origins: tuple[str, ...]
    rate_limit_per_minute: int
    assemble_ttl_seconds: int
    xai_model: str
    max_session_seconds: int
    basic_user: str | None = None
    basic_password: str | None = None

    @classmethod
    def from_env(cls, env_file: Path | None = None) -> Settings:
        from moviexfone.paths import data_dir

        _load_dotenv(env_file or Path.cwd() / ".env")
        key = os.environ.get("XAI_API_KEY", "").strip() or None
        origins = os.environ.get(
            "MOVIEXFONE_CORS_ORIGINS",
            "http://127.0.0.1:8000,http://localhost:8000",
        )
        secret = os.environ.get("MOVIEXFONE_TOOL_SECRET", "").strip() or None
        return cls(
            xai_api_key=key,
            live_voice=_flag("MOVIEXFONE_LIVE_VOICE"),
            live_data=_flag("MOVIEXFONE_LIVE_DATA"),
            tool_secret=secret,
            db_path=Path(os.environ.get("MOVIEXFONE_DB", "var/moviexfone.db")),
            data_dir=data_dir(),
            bind=os.environ.get("MOVIEXFONE_BIND", "127.0.0.1"),
            port=int(os.environ.get("MOVIEXFONE_PORT", "8000")),
            cors_origins=tuple(part.strip() for part in origins.split(",") if part.strip()),
            rate_limit_per_minute=int(os.environ.get("MOVIEXFONE_RATE_LIMIT", "60")),
            assemble_ttl_seconds=int(os.environ.get("MOVIEXFONE_ASSEMBLE_TTL", "900")),
            xai_model=os.environ.get("MOVIEXFONE_XAI_MODEL", "grok-voice-think-fast-2.0"),
            max_session_seconds=int(os.environ.get("MOVIEXFONE_MAX_SESSION", "900")),
            basic_user=os.environ.get("MOVIEXFONE_BASIC_USER", "").strip() or None,
            basic_password=os.environ.get("MOVIEXFONE_BASIC_PASSWORD", "").strip() or None,
        )

    @property
    def voice_mode(self) -> str:
        if self.live_voice and self.xai_api_key:
            return "live"
        return "mock"
