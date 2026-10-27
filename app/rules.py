"""Business rules. Every number comes from policy_parameters; every decision is
made here in Python, not by the language model.
"""

from __future__ import annotations

import re
from datetime import date

from dateutil.relativedelta import relativedelta

VALID_REASONS = ("changed_mind", "defective")


class PolicyParameters:
    """Typed access to the policy_parameters table."""

    def __init__(self, rows: dict[str, dict]):
        self._rows = rows

    def get(self, key: str) -> str | None:
        row = self._rows.get(key)
        return None if row is None else str(row.get("value"))

    def require_int(self, key: str) -> int:
        value = self.get(key)
        if value is None or not str(value).strip():
            raise KeyError(f"policy_parameters is missing {key!r}")
        parsed = self.get_int(key)
        if parsed is None:
            raise KeyError(f"policy_parameters[{key!r}] is not a number: {value!r}")
        return parsed

    def get_int(self, key: str) -> int | None:
        value = self.get(key)
        if value is None or not str(value).strip():
            return None
        match = re.search(r"-?\d+(?:\.\d+)?", str(value))
        if not match:
            return None
        try:
            return int(float(match.group(0)))
        except ValueError:
            return None

    def source(self, key: str) -> dict | None:
        row = self._rows.get(key)
        if not row:
            return None
        return {
            "doc_id": row.get("source_doc_id"),
            "section": row.get("source_section"),
        }


def enrich_order(order: dict, app_today: date, params: PolicyParameters) -> dict:
    """Add delivery date, days since delivery and warranty fields to an order."""
    shipment = order.get("shipment") or {}
    delivery = _delivery_date(order)
    days = (app_today - delivery).days if delivery else None

    order["delivery_date"] = delivery.isoformat() if delivery else None
    order["days_since_delivery"] = days
    order["shipment_status"] = shipment.get("status")

    for item in order.get("items", []):
        months = warranty_months(item.get("warranty_class"), params)
        end = warranty_end_date(delivery, months)
        item["warranty_months"] = months
        item["warranty_end"] = end.isoformat() if end else None
        item["warranty_active"] = bool(end and app_today <= end)
    return order


def _delivery_date(order: dict):
    shipment = order.get("shipment") or {}
    value = shipment.get("delivered_at")
    if value:
        from .util import parse_date

        return parse_date(value)
    return None


def warranty_months(warranty_class: str | None, params: PolicyParameters) -> int:
    normalized = str(warranty_class or "").lower()
    if normalized == "standard":
        return params.require_int("standard_warranty_months")
    if normalized == "extended":
        return params.require_int("extended_warranty_months")
    return 0


def warranty_end_date(delivery: date | None, months: int | None) -> date | None:
    """Same calendar day N months later; relativedelta handles month-end."""
    if delivery is None or not months:
        return None
    return delivery + relativedelta(months=months)


def _error(code: str, message: str, **extra) -> dict:
    return {"eligible": False, "error_code": code, "message": message, **extra}


def evaluate_return(
    order: dict,
    item_ids: list[str],
    reason: str,
    params: PolicyParameters,
) -> dict:
    """Apply the return checks in the exact order required by the brief."""
    items_by_id = {item["item_id"]: item for item in order.get("items", [])}

    if not isinstance(item_ids, list) or not item_ids:
        return _error("INVALID_ITEM", "No items were given")

    missing = [item_id for item_id in item_ids if item_id not in items_by_id]
    if missing:
        return _error(
            "INVALID_ITEM", f"Item {missing[0]} is not in this order"
        )

    if reason not in VALID_REASONS:
        return _error(
            "INVALID_REASON",
            "Reason must be changed_mind or defective",
        )

    if order.get("status") != "delivered" or not order.get("delivery_date"):
        return _error("NOT_DELIVERED", "This order has not been delivered")

    window = params.require_int("return_window_days")
    days = order.get("days_since_delivery")
    items = [items_by_id[item_id] for item_id in item_ids]

    # Check 2: never-returnable products (gift cards, software licences).
    for item in items:
        if str(item.get("returnable") or "").lower() == "no":
            return _error(
                "NON_RETURNABLE",
                f"{item.get('name', item['item_id'])} cannot be returned",
                item_id=item["item_id"],
            )

    # Check 3: return window.
    if days is None or days > window:
        return _error(
            "RETURN_WINDOW_EXPIRED",
            f"The {window}-day return window has expired",
            days_since_delivery=days,
            return_window_days=window,
        )

    # Check 4: opened, unopened-only products, change of mind.
    if reason == "changed_mind":
        for item in items:
            if item.get("opened") and str(item.get("returnable") or "").lower() == "unopened_only":
                return _error(
                    "OPENED_NOT_RETURNABLE",
                    f"{item.get('name', item['item_id'])} was opened and can "
                    "only be returned while sealed for a change of mind",
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
    order: dict,
    item_ids: list[str] | None,
    params: PolicyParameters,
) -> dict:
    """Can this order (or these items) make a warranty claim today?"""
    if order.get("status") != "delivered" or not order.get("delivery_date"):
        return _error("NOT_DELIVERED", "This order has not been delivered")

    items = order.get("items", [])
    if item_ids:
        by_id = {item["item_id"]: item for item in items}
        missing = [item_id for item_id in item_ids if item_id not in by_id]
        if missing:
            return _error("INVALID_ITEM", f"Item {missing[0]} is not in this order")
        items = [by_id[item_id] for item_id in item_ids]
    if not items:
        return _error("INVALID_ITEM", "No items were given")

    covered, expired, no_warranty = [], [], []
    for item in items:
        if str(item.get("warranty_class") or "").lower() == "none" or not item.get("warranty_months"):
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
        items=[{"item_id": item["item_id"], "name": item.get("name")} for item in items],
    )


def evaluate_cancellation(order: dict) -> dict:
    if order.get("status") == "processing":
        return {"eligible": True, "order_id": order.get("order_id")}
    if order.get("status") == "cancelled":
        return _error("ORDER_ALREADY_SHIPPED", "This order is already cancelled")
    return _error(
        "ORDER_ALREADY_SHIPPED",
        "This order has already shipped and cannot be cancelled",
    )
