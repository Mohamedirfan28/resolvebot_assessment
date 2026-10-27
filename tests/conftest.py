"""Shared pytest fixtures.

Every test gets a fresh copy of the supplied SQLite database. The original
``data/shopsphere.db`` is never opened for writing by the suite.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"


def _prepare_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Path:
    db_path = tmp_path / "shopsphere.db"
    shutil.copy2(DATA / "shopsphere.db", db_path)
    values = {
        "DB_PATH": str(db_path),
        "POLICIES_PATH": str(DATA / "policies.md"),
        "MANIFEST_PATH": str(DATA / "manifest.json"),
        "APP_TODAY": "2026-07-01",
        "LLM_PROVIDER": "none",
        "LLM_AGENT": "never",
        "LLM_MODEL": "",
        "LLM_BASE_URL": "",
        "LLM_API_KEY": "",
        "LOG_LEVEL": "WARNING",
    }
    values.update({key: str(value) for key, value in overrides.items()})
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return db_path


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Deterministic app with a private database copy."""
    from fastapi.testclient import TestClient

    db_path = _prepare_env(tmp_path, monkeypatch)
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, db_path=db_path, settings=app.state.settings)


@pytest.fixture()
def agent_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """App configured to run the optional LLM tool loop (no network calls)."""
    from fastapi.testclient import TestClient

    db_path = _prepare_env(
        tmp_path,
        monkeypatch,
        LLM_PROVIDER="openai",
        OPENAI_API_KEY="test-key-not-real",
        LLM_AGENT="always",
    )
    from app.main import create_app

    app = create_app()
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, db_path=db_path, settings=app.state.settings)


@pytest.fixture()
def set_parameter():
    """Update a business number in the private database copy."""

    def _set(db_path: Path, key: str, value: str) -> None:
        conn = sqlite3.connect(db_path)
        try:
            updated = conn.execute(
                "UPDATE policy_parameters SET value = ? WHERE key = ?",
                (value, key),
            ).rowcount
            if not updated:
                raise AssertionError(f"No policy_parameters row for {key!r}")
            conn.commit()
        finally:
            conn.close()

    return _set


@pytest.fixture()
def row_count():
    def _count(db_path: Path, table: str) -> int:
        conn = sqlite3.connect(db_path)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    return _count
