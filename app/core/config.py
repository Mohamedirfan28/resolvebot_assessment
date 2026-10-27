"""Application settings loaded from environment variables.

The assessment brief requires every path to default to ``./data`` and the
business date to come from ``APP_TODAY`` (never the machine clock).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_APP_TODAY = date(2026, 7, 1)
_TRUE = {"1", "true", "yes", "on"}


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so no extra dependency is needed.

    Existing environment variables always win over values in the file.
    """
    if not path.is_file():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _as_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in _TRUE


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _as_date(name: str, default: date) -> date:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be YYYY-MM-DD, got {raw!r}") from exc


@dataclass(frozen=True, slots=True)
class Settings:
    policies_path: Path
    manifest_path: Path
    db_path: Path
    app_today: date
    llm_provider: str
    llm_model: str
    llm_base_url: str
    llm_api_key: str
    llm_agent: str
    llm_polish: bool
    host: str
    port: int
    max_tool_calls: int
    log_level: str

    @property
    def llm_configured(self) -> bool:
        return self.llm_provider not in {"", "none"} and bool(self.llm_api_key)

    @property
    def agent_enabled(self) -> bool:
        """Whether the optional LLM tool loop should run for normal turns.

        ``auto`` (the default) enables it only when a provider and key are both
        configured. The deterministic planner remains available as a fallback
        and is always used for confirmation turns and tested business rules.
        """
        if self.llm_agent == "always":
            return self.llm_configured
        if self.llm_agent == "never":
            return False
        return self.llm_configured


def get_settings() -> Settings:
    _load_dotenv(Path(".env"))

    provider = os.getenv("LLM_PROVIDER", "none").strip().lower()
    if provider not in {"none", "openai", "anthropic", "google"}:
        raise ValueError(
            "LLM_PROVIDER must be one of: none, openai, anthropic, google"
        )

    mode = os.getenv("LLM_AGENT", "auto").strip().lower()
    if mode in {"1", "true", "yes", "on"}:
        mode = "always"
    elif mode in {"0", "false", "no", "off"}:
        mode = "never"
    if mode not in {"auto", "always", "never"}:
        raise ValueError("LLM_AGENT must be auto, always, never, 0 or 1")

    api_key = os.getenv(f"{provider.upper()}_API_KEY", "").strip() if provider != "none" else ""
    # Also accept the generic key name used by some deployments.
    api_key = api_key or os.getenv("LLM_API_KEY", "").strip()

    settings = Settings(
        policies_path=Path(os.getenv("POLICIES_PATH", "./data/policies.md")).expanduser(),
        manifest_path=Path(os.getenv("MANIFEST_PATH", "./data/manifest.json")).expanduser(),
        db_path=Path(os.getenv("DB_PATH", "./data/shopsphere.db")).expanduser(),
        app_today=_as_date("APP_TODAY", DEFAULT_APP_TODAY),
        llm_provider=provider,
        llm_model=os.getenv("LLM_MODEL", "").strip(),
        llm_base_url=os.getenv("LLM_BASE_URL", "").strip(),
        llm_api_key=api_key,
        llm_agent=mode,
        llm_polish=_as_bool("LLM_POLISH", False),
        host=os.getenv("HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=_as_int("PORT", 8000),
        max_tool_calls=max(1, _as_int("MAX_TOOL_CALLS", 6)),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
    return settings
