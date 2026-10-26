"""SQLite access.

The customer ID passed to every function here comes from the request header,
never from the language model.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .util import parse_date


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def customer_exists(conn: sqlite3.Connection, customer_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return row is not None


def get_customer(conn: sqlite3.Connection, customer_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM customers WHERE customer_id = ?", (customer_id,)
    ).fetchone()
    return dict(row) if row else None


def fetch_orders(
    conn: sqlite3.Connection,
    customer_id: str,
    order_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    """Return the customer's orders, with shipment and item details attached."""
    sql = "SELECT * FROM orders WHERE customer_id = ?"
    args: list = [customer_id]
    if order_id:
        sql += " AND order_id = ?"
        args.append(order_id)
    if status:
        sql += " AND status = ?"
        args.append(status)
    sql += " ORDER BY order_date DESC"

    orders = [dict(row) for row in conn.execute(sql, args).fetchall()]
    for order in orders:
        order["shipment"] = _fetch_shipment(conn, order["order_id"])
        order["items"] = _fetch_items(conn, order["order_id"])
    return orders


def _fetch_shipment(conn: sqlite3.Connection, order_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM shipments WHERE order_id = ?", (order_id,)
    ).fetchone()
    return dict(row) if row else None


def _fetch_items(conn: sqlite3.Connection, order_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT i.item_id, i.order_id, i.product_id, i.qty, i.unit_price,
               i.opened, i.defect_reported,
               p.name, p.brand, p.category, p.price, p.returnable, p.warranty_class
        FROM order_items i
        JOIN products p ON p.product_id = i.product_id
        WHERE i.order_id = ?
        ORDER BY i.item_id
        """,
        (order_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_policy_parameters(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM policy_parameters").fetchall()
    return {row["key"]: dict(row) for row in rows}


def insert_return_request(
    conn: sqlite3.Connection,
    customer_id: str,
    order_id: str,
    item_ids: list[str],
    reason: str,
) -> str:
    request_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO return_requests
            (request_id, order_id, customer_id, item_ids, reason, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            request_id,
            order_id,
            customer_id,
            json.dumps(item_ids),
            reason,
            "requested",
            _now_iso(),
        ),
    )
    conn.commit()
    return request_id


def insert_support_ticket(
    conn: sqlite3.Connection,
    customer_id: str,
    order_id: str,
    ticket_type: str,
    summary: str,
) -> str:
    ticket_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO support_tickets
            (ticket_id, order_id, customer_id, ticket_type, summary, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            order_id,
            customer_id,
            ticket_type,
            summary,
            "open",
            _now_iso(),
        ),
    )
    conn.commit()
    return ticket_id


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def customer_has_order(conn: sqlite3.Connection, customer_id: str, order_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM orders WHERE order_id = ? AND customer_id = ?",
        (order_id, customer_id),
    ).fetchone()
    return row is not None


def delivery_date(order: dict):
    shipment = order.get("shipment") or {}
    return parse_date(shipment.get("delivered_at"))
