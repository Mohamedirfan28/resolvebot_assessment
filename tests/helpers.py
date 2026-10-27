"""Small HTTP/database helpers for the test suite."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def post_chat(client, customer_id: str, session_id: str, message: str):
    return client.post(
        "/chat",
        headers={"X-Customer-Id": customer_id},
        json={"session_id": session_id, "message": message},
    )


def json_chat(client, customer_id: str, session_id: str, message: str) -> dict:
    response = post_chat(client, customer_id, session_id, message)
    assert response.status_code == 200, response.text
    return response.json()


def count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def tool_names(payload: dict) -> list[str]:
    return [call["name"] for call in payload.get("tool_calls", [])]
