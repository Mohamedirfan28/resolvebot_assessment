"""Configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_APP_TODAY = "2026-07-01"


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so `python main.py` works without extra packages.

    Existing environment variables always win.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser()


@dataclass(frozen=True)
class Settings:
    policies_path: Path
    manifest_path: Path
    db_path: Path
    app_today: date
    llm_provider: str
    llm_model: str
    llm_base_url: str
    llm_polish: bool
    llm_agent: bool
    openai_api_key: str
    anthropic_api_key: str
    google_api_key: str
    host: str
    port: int
    max_tool_calls: int

    @property
    def llm_api_key(self) -> str:
        return {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "google": self.google_api_key,
        }.get(self.llm_provider, "")

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider not in {"", "none"} and bool(self.llm_api_key)


def get_settings() -> Settings:
    _load_dotenv(Path(".env"))

    raw_today = os.getenv("APP_TODAY", DEFAULT_APP_TODAY).strip()
    try:
        app_today = date.fromisoformat(raw_today)
    except ValueError as exc:
        raise ValueError(f"APP_TODAY must be YYYY-MM-DD, got {raw_today!r}") from exc

    provider = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if provider not in {"none", "openai", "anthropic", "google"}:
        raise ValueError(
            "LLM_PROVIDER must be one of: none, openai, anthropic, google"
        )

    return Settings(
        policies_path=_env_path("POLICIES_PATH", "./data/policies.md"),
        manifest_path=_env_path("MANIFEST_PATH", "./data/manifest.json"),
        db_path=_env_path("DB_PATH", "./data/shopsphere.db"),
        app_today=app_today,
        llm_provider=provider,
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
        llm_polish=_env_bool("LLM_POLISH", False),
        llm_agent=_env_bool("LLM_AGENT", False),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        google_api_key=os.getenv("GOOGLE_API_KEY", "").strip(),
        host=os.getenv("HOST", "0.0.0.0").strip(),
        port=int(os.getenv("PORT", "8000")),
        max_tool_calls=int(os.getenv("MAX_TOOL_CALLS", "6")),
    )
