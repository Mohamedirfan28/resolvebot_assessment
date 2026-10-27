"""Tool-level tests: owner scoping, computed fields, validation and writes."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest

from app.db import repository
from app.services.tools import ToolContext, ToolLimitExceeded

from tests.helpers import count_rows


def make_context(api, customer_id: str, *, max_tool_calls: int = 10) -> ToolContext:
    conn = repository.connect(api.db_path)
    return ToolContext(
        conn,
        api.settings,
        api.app.state.policy_index,
        customer_id,
        max_tool_calls=max_tool_calls,
    )


def test_get_orders_returns_computed_delivery_and_warranty_fields(api):
    ctx = make_context(api, "C1009")
    try:
        result = ctx.get_orders(order_id="SS-10458")
        assert "orders" in result
        order = result["orders"][0]
        assert order["order_id"] == "SS-10458"
        assert order["delivery_date"] == "2026-06-21"
        assert order["days_since_delivery"] == 10
        item = order["items"][0]
        assert item["warranty_end"] == "2027-06-21"
        assert item["warranty_active"] is True
        assert item["opened"] is False
        assert ctx.calls == [
            {"name": "get_orders", "args": {"order_id": "SS-10458"}, "status": "success"}
        ]
    finally:
        ctx.conn.close()


def test_get_orders_is_scoped_to_the_customer(api):
    ctx = make_context(api, "C1010")
    try:
        result = ctx.get_orders(order_id="SS-10458")
        assert result["error_code"] == "ORDER_NOT_FOUND"
        assert "orders" not in result
        assert ctx.calls[0]["status"] == "error"
        assert ctx.calls[0]["error_code"] == "ORDER_NOT_FOUND"
    finally:
        ctx.conn.close()


def test_get_orders_without_id_returns_only_own_orders(api):
    ctx = make_context(api, "C1009")
    try:
        result = ctx.get_orders()
        ids = {order["order_id"] for order in result["orders"]}
        assert ids == {"SS-10458", "SS-10512"}
    finally:
        ctx.conn.close()


def test_get_orders_status_filter(api):
    ctx = make_context(api, "C1039")
    try:
        result = ctx.get_orders(status="processing")
        assert [order["order_id"] for order in result["orders"]] == ["SS-10530"]
    finally:
        ctx.conn.close()


def test_check_return_eligibility_and_error_logging(api):
    ctx = make_context(api, "C1011")
    try:
        eligible = ctx.check_return_eligibility(
            "SS-10505", ["SS-10505-1"], "defective"
        )
        assert eligible["eligible"] is True
        assert ctx.calls[-1]["status"] == "success"

        blocked = ctx.check_return_eligibility(
            "SS-10505", ["SS-10505-1"], "changed_mind"
        )
        assert blocked["error_code"] == "OPENED_NOT_RETURNABLE"
        assert ctx.calls[-1]["status"] == "error"
        assert ctx.calls[-1]["error_code"] == "OPENED_NOT_RETURNABLE"
    finally:
        ctx.conn.close()


def test_create_return_request_rechecks_and_writes_once(api, set_parameter):
    set_parameter(api.db_path, "return_window_days", "10")
    ctx = make_context(api, "C1009")
    try:
        result = ctx.create_return_request(
            "SS-10458", ["SS-10458-1"], "changed_mind"
        )
        assert result["status"] == "requested"
        assert result["order_id"] == "SS-10458"
        assert count_rows(api.db_path, "return_requests") == 1

        conn = sqlite3.connect(api.db_path)
        try:
            row = conn.execute(
                "SELECT order_id, customer_id, item_ids, reason, status "
                "FROM return_requests"
            ).fetchone()
        finally:
            conn.close()
        assert row == (
            "SS-10458",
            "C1009",
            '["SS-10458-1"]',
            "changed_mind",
            "requested",
        )
    finally:
        ctx.conn.close()


def test_create_return_request_refuses_expired_window(api, set_parameter):
    set_parameter(api.db_path, "return_window_days", "9")
    ctx = make_context(api, "C1009")
    try:
        result = ctx.create_return_request(
            "SS-10458", ["SS-10458-1"], "changed_mind"
        )
        assert result["error_code"] == "RETURN_WINDOW_EXPIRED"
        assert [call["status"] for call in ctx.calls] == ["error"]
        assert count_rows(api.db_path, "return_requests") == 0
    finally:
        ctx.conn.close()


@pytest.mark.parametrize(
    ("order_id", "item_ids", "reason", "expected"),
    [
        ("SS-99999", ["SS-99999-1"], "changed_mind", "ORDER_NOT_FOUND"),
        ("SS-10458", ["SS-10458-9"], "changed_mind", "INVALID_ITEM"),
        ("SS-10458", ["SS-10458-1"], "because", "INVALID_REASON"),
    ],
)
def test_create_return_request_validation_errors(api, order_id, item_ids, reason, expected):
    ctx = make_context(api, "C1009")
    try:
        result = ctx.create_return_request(order_id, item_ids, reason)
        assert result["error_code"] == expected
        assert count_rows(api.db_path, "return_requests") == 0
    finally:
        ctx.conn.close()


def test_create_return_request_cannot_write_another_customers_order(api):
    ctx = make_context(api, "C1010")
    try:
        result = ctx.create_return_request(
            "SS-10458", ["SS-10458-1"], "changed_mind"
        )
        assert result["error_code"] == "ORDER_NOT_FOUND"
        assert count_rows(api.db_path, "return_requests") == 0
    finally:
        ctx.conn.close()


def test_create_support_ticket_warranty_and_cancellation(api):
    warranty_ctx = make_context(api, "C1018")
    try:
        warranty = warranty_ctx.create_support_ticket(
            "SS-09871", "warranty", "Laptop will not charge"
        )
        assert warranty["status"] == "open"
        assert warranty["ticket_type"] == "warranty"
    finally:
        warranty_ctx.conn.close()

    cancel_ctx = make_context(api, "C1039")
    try:
        cancellation = cancel_ctx.create_support_ticket(
            "SS-10530", "cancellation", "Customer requested cancellation"
        )
        assert cancellation["status"] == "open"
        assert cancellation["ticket_type"] == "cancellation"
    finally:
        cancel_ctx.conn.close()

    assert count_rows(api.db_path, "support_tickets") == 2


def test_create_support_ticket_refuses_invalid_states(api):
    shipped = make_context(api, "C1009")
    try:
        result = shipped.create_support_ticket(
            "SS-10512", "cancellation", "Cancel it"
        )
        assert result["error_code"] == "ORDER_ALREADY_SHIPPED"
    finally:
        shipped.conn.close()

    expired = make_context(api, "C1017")
    try:
        result = expired.create_support_ticket(
            "SS-09934", "warranty", "Phone is broken"
        )
        assert result["error_code"] == "WARRANTY_EXPIRED"
    finally:
        expired.conn.close()

    no_warranty = make_context(api, "C1025")
    try:
        result = no_warranty.create_support_ticket(
            "SS-10106", "warranty", "Software is broken"
        )
        assert result["error_code"] == "NO_WARRANTY"
    finally:
        no_warranty.conn.close()

    invalid = make_context(api, "C1009")
    try:
        result = invalid.create_support_ticket("SS-10458", "refund", "Nope")
        assert result["error_code"] == "INVALID_TICKET_TYPE"
    finally:
        invalid.conn.close()

    assert count_rows(api.db_path, "support_tickets") == 0


def test_create_support_ticket_cannot_write_another_customers_order(api):
    ctx = make_context(api, "C1010")
    try:
        result = ctx.create_support_ticket(
            "SS-10458", "warranty", "My speaker is broken"
        )
        assert result["error_code"] == "ORDER_NOT_FOUND"
        assert count_rows(api.db_path, "support_tickets") == 0
    finally:
        ctx.conn.close()


def test_tool_limit_is_enforced(api):
    ctx = make_context(api, "C1009", max_tool_calls=1)
    try:
        ctx.get_orders()
        with pytest.raises(ToolLimitExceeded):
            ctx.get_orders()
    finally:
        ctx.conn.close()
