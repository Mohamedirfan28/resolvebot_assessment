"""ShopSphere business rules.

All decisions are made here in Python. All business numbers come from the
``policy_parameters`` table, never from constants in this module.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from dateutil.relativedelta import relativedelta

VALID_REASONS = ("changed_mind", "defective")
VALID_TICKET_TYPES = ("warranty", "cancellation")


class PolicyParameterError(RuntimeError):
    """A required business number is missing or malformed."""


class PolicyParameters:
    """Typed read access to the policy_parameters table."""

    def __init__(self, rows: dict[str, dict[str, Any]]):
        self._rows = rows

    def get(self, key: str) -> str | None:
        row = self._rows.get(key)
        return None if row is None else str(row.get("value"))

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        if value is None or not value.strip():
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        if not match:
            return None
        try:
            return int(float(match.group(0)))
        except ValueError:
            return None

    def require_int(self, key: str) -> int:
        value = self.get_int(key)
        if value is None:
            raise PolicyParameterError(
                f"policy_parameters is missing a usable {key!r} value"
            )
        return value

    def source(self, key: str) -> dict[str, str] | None:
        row = self._rows.get(key)
        if not row:
            return None
        return {
            "doc_id": str(row.get("source_doc_id") or ""),
            "section": str(row.get("source_section") or ""),
        }


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def warranty_months(warranty_class: str | None, params: PolicyParameters) -> int:
    value = str(warranty_class or "").strip().lower()
    if value == "standard":
        return params.require_int("standard_warranty_months")
    if value == "extended":
        return params.require_int("extended_warranty_months")
    return 0


def warranty_end_date(delivery: date | None, months: int | None) -> date | None:
    """Same calendar day N months later; relativedelta handles month-end."""
    if delivery is None or not months or months <= 0:
        return None
    return delivery + relativedelta(months=months)


def delivery_date(order: dict) -> date | None:
    from app.utils import parse_date

    shipment = order.get("shipment") or {}
    return parse_date(shipment.get("delivered_at"))


def enrich_order(
    order: dict, app_today: date, params: PolicyParameters
) -> dict:
    """Add computed delivery and warranty fields used by tools/replies."""
    from app.utils import parse_date

    shipment = order.get("shipment") or {}
    delivered = delivery_date(order)
    order["delivery_date"] = delivered.isoformat() if delivered else None
    order["days_since_delivery"] = (
        (app_today - delivered).days if delivered else None
    )
    order["shipment_status"] = shipment.get("status")
    order["eta"] = shipment.get("eta")
    order["original_eta"] = shipment.get("original_eta")
    order["carrier"] = shipment.get("carrier")
    order["tracking_no"] = shipment.get("tracking_no")

    for item in order.get("items", []):
        months = warranty_months(item.get("warranty_class"), params)
        end = warranty_end_date(delivered, months)
        item["warranty_months"] = months
        item["warranty_end"] = end.isoformat() if end else None
        item["warranty_active"] = bool(end and app_today <= end)
        item["opened"] = bool(_as_int(item.get("opened")))
        item["qty"] = _as_int(item.get("qty")) or 1

    # Convenience for clients; not a business decision.
    if shipment.get("eta"):
        order["eta"] = parse_date(shipment.get("eta")).isoformat() if parse_date(shipment.get("eta")) else None
    return order


def _error(code: str, message: str, **extra: Any) -> dict:
    return {"eligible": False, "error_code": code, "message": message, **extra}


def evaluate_return(
    order: dict | None,
    item_ids: list[str] | None,
    reason: str,
    params: PolicyParameters,
) -> dict:
    """Apply the four return checks in the order required by the brief."""
    if order is None:
        return _error("ORDER_NOT_FOUND", "Order not found on this account")

    if not isinstance(item_ids, list) or not item_ids:
        return _error("INVALID_ITEM", "At least one item_id is required")

    items_by_id = {item["item_id"]: item for item in order.get("items", [])}
    missing = [item_id for item_id in item_ids if item_id not in items_by_id]
    if missing:
        return _error(
            "INVALID_ITEM", f"Item {missing[0]} is not in this order"
        )

    if reason not in VALID_REASONS:
        return _error(
            "INVALID_REASON", "Reason must be changed_mind or defective"
        )

    if order.get("status") != "delivered" or not order.get("delivery_date"):
        return _error("NOT_DELIVERED", "This order has not been delivered")

    items = [items_by_id[item_id] for item_id in item_ids]
    days = order.get("days_since_delivery")

    # 2. Never-returnable products (gift cards, software licences).
    for item in items:
        if str(item.get("returnable") or "").lower() == "no":
            return _error(
                "NON_RETURNABLE",
                f"{item.get('name') or item['item_id']} cannot be returned",
                item_id=item["item_id"],
            )

    # 3. Return window, inclusive of the final day.
    window = params.require_int("return_window_days")
    if days is None or days < 0 or days > window:
        return _error(
            "RETURN_WINDOW_EXPIRED",
            f"The {window}-day return window has expired",
            days_since_delivery=days,
            return_window_days=window,
        )

    # 4. Opened item whose product is returnable only while sealed.
    if reason == "changed_mind":
        for item in items:
            if item.get("opened") and str(item.get("returnable") or "").lower() == "unopened_only":
                return _error(
                    "OPENED_NOT_RETURNABLE",
                    (
                        f"{item.get('name') or item['item_id']} was opened and "
                        "can only be returned while sealed for a change of mind"
                    ),
                    item_id=item["item_id"],
                )

    return {
        "eligible": True,
        "order_id": order["order_id"],
        "item_ids": item_ids,
        "reason": reason,
        "days_since_delivery": days,
        "return_window_days": window,
        "items": [
            {
                "item_id": item["item_id"],
                "name": item.get("name"),
                "opened": bool(item.get("opened")),
                "returnable": item.get("returnable"),
            }
            for item in items
        ],
    }


def evaluate_warranty(
    order: dict | None,
    item_ids: list[str] | None,
    params: PolicyParameters,
) -> dict:
    """Can this order (or the chosen items) make a warranty claim today?"""
    if order is None:
        return _error("ORDER_NOT_FOUND", "Order not found on this account")

    if order.get("status") != "delivered" or not order.get("delivery_date"):
        return _error("NOT_DELIVERED", "This order has not been delivered")

    items = list(order.get("items", []))
    if item_ids:
        by_id = {item["item_id"]: item for item in items}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            return _error("INVALID_ITEM", f"Item {missing[0]} is not in this order")
        items = [by_id[item_id] for item_id in item_ids]
    if not items:
        return _error("INVALID_ITEM", "No items were found in this order")

    covered: list[dict] = []
    expired: list[dict] = []
    no_warranty: list[dict] = []
    for item in items:
        warranty_class = str(item.get("warranty_class") or "").lower()
        if warranty_class == "none" or not item.get("warranty_months"):
            no_warranty.append(item)
        elif item.get("warranty_active"):
            covered.append(item)
        else:
            expired.append(item)

    if covered:
        return {
            "eligible": True,
            "order_id": order["order_id"],
            "covered": [
                {
                    "item_id": item["item_id"],
                    "name": item.get("name"),
                    "warranty_end": item.get("warranty_end"),
                    "warranty_months": item.get("warranty_months"),
                }
                for item in covered
            ],
            "expired": [
                {
                    "item_id": item["item_id"],
                    "name": item.get("name"),
                    "warranty_end": item.get("warranty_end"),
                }
                for item in expired
            ],
            "no_warranty": [
                {"item_id": item["item_id"], "name": item.get("name")}
                for item in no_warranty
            ],
        }

    if expired:
        return _error(
            "WARRANTY_EXPIRED",
            "The warranty for these items has expired",
            expired=[
                {
                    "item_id": item["item_id"],
                    "name": item.get("name"),
                    "warranty_end": item.get("warranty_end"),
                }
                for item in expired
            ],
        )
    return _error(
        "NO_WARRANTY",
        "These products do not have a warranty",
        items=[
            {"item_id": item["item_id"], "name": item.get("name")}
            for item in items
        ],
    )


def evaluate_cancellation(order: dict | None) -> dict:
    if order is None:
        return _error("ORDER_NOT_FOUND", "Order not found on this account")
    if order.get("status") == "processing":
        return {"eligible": True, "order_id": order.get("order_id")}
    if order.get("status") == "cancelled":
        return _error(
            "ORDER_ALREADY_SHIPPED", "This order has already been cancelled"
        )
    return _error(
        "ORDER_ALREADY_SHIPPED",
        "This order has already shipped and cannot be cancelled",
    )
