from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _split_aliases(value: str | None) -> tuple[str, ...]:
    raw = value or "@ai,/ai"
    aliases = tuple(part.strip() for part in raw.split(",") if part.strip())
    return aliases or ("@ai", "/ai")


@dataclass(frozen=True)
class Settings:
    aitunnel_api_key: str | None
    aitunnel_model: str
    aitunnel_base_url: str
    pachca_access_token: str | None
    pachca_signing_secret: str | None
    pachca_bot_aliases: tuple[str, ...]
    agent_timezone: str
    max_context_chars: int
    max_messages_per_scan: int
    dry_run: bool
    database_path: Path
    improvements_log_path: Path


def load_settings() -> Settings:
    data_dir = Path(os.getenv("DATA_DIR", "data"))
    database_path = Path(os.getenv("DATABASE_PATH", str(data_dir / "agent.db")))
    database_path.parent.mkdir(parents=True, exist_ok=True)
    improvements_log_path = Path(os.getenv("IMPROVEMENTS_LOG_PATH", str(database_path.parent / "agent_improvements.md")))
    improvements_log_path.parent.mkdir(parents=True, exist_ok=True)

    return Settings(
        aitunnel_api_key=os.getenv("AITUNNEL_API_KEY"),
        aitunnel_model=os.getenv("AITUNNEL_MODEL", "deepseek-chat"),
        aitunnel_base_url=os.getenv("AITUNNEL_BASE_URL", "https://api.aitunnel.ru/v1").rstrip("/"),
        pachca_access_token=os.getenv("PACHCA_ACCESS_TOKEN"),
        pachca_signing_secret=os.getenv("PACHCA_SIGNING_SECRET"),
        pachca_bot_aliases=_split_aliases(os.getenv("PACHCA_BOT_ALIASES")),
        agent_timezone=os.getenv("AGENT_TIMEZONE", "Europe/Moscow"),
        max_context_chars=int(os.getenv("MAX_CONTEXT_CHARS", "8000")),
        max_messages_per_scan=int(os.getenv("MAX_MESSAGES_PER_SCAN", "200")),
        dry_run=_as_bool(os.getenv("PACHCA_DRY_RUN")),
        database_path=database_path,
        improvements_log_path=improvements_log_path,
    )
