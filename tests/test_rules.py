"""Unit tests for the business rules (no server needed).

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date

from app import rules


def params(**overrides):
    rows = {
        "return_window_days": {"key": "return_window_days", "value": "30"},
        "standard_warranty_months": {"key": "standard_warranty_months", "value": "12"},
        "extended_warranty_months": {"key": "extended_warranty_months", "value": "24"},
    }
    for key, value in overrides.items():
        rows[key] = {"key": key, "value": str(value)}
    return rules.PolicyParameters(rows)


def make_order(
    delivery="2026-06-01",
    today=date(2026, 7, 1),
    status="delivered",
    items=None,
):
    items = items or [
        {
            "item_id": "SS-1-1",
            "name": "Test Speaker",
            "returnable": "yes",
            "unopened_only": False,
            "opened": 0,
            "warranty_class": "standard",
        }
    ]
    for item in items:
        item.setdefault("warranty_months", 12)
        item.setdefault("warranty_end", "2027-06-01")
        item.setdefault("warranty_active", True)
    return {
        "order_id": "SS-1",
        "status": status,
        "delivery_date": delivery,
        "days_since_delivery": (today - date.fromisoformat(delivery)).days
        if delivery
        else None,
        "items": items,
    }


def item(**overrides):
    base = {
        "item_id": "SS-1-1",
        "name": "Test Item",
        "returnable": "yes",
        "opened": 0,
        "warranty_class": "standard",
    }
    base.update(overrides)
    return base


class ReturnRuleTests(unittest.TestCase):
    def test_day_30_is_allowed(self):
        order = make_order(delivery="2026-06-01", today=date(2026, 7, 1))
        result = rules.evaluate_return(order, ["SS-1-1"], "changed_mind", params())
        self.assertTrue(result["eligible"], result)
        self.assertEqual(result["days_since_delivery"], 30)

    def test_day_31_is_too_late(self):
        order = make_order(delivery="2026-06-01", today=date(2026, 7, 2))
        result = rules.evaluate_return(order, ["SS-1-1"], "changed_mind", params())
        self.assertEqual(result["error_code"], "RETURN_WINDOW_EXPIRED")

    def test_window_comes_from_policy_parameters(self):
        order = make_order(delivery="2026-06-01", today=date(2026, 7, 1))
        result = rules.evaluate_return(
            order, ["SS-1-1"], "changed_mind", params(return_window_days=15)
        )
        self.assertEqual(result["error_code"], "RETURN_WINDOW_EXPIRED")

    def test_opened_earbuds_changed_mind(self):
        order = make_order(
            items=[item(returnable="unopened_only", opened=1, name="Test Buds")]
        )
        result = rules.evaluate_return(order, ["SS-1-1"], "changed_mind", params())
        self.assertEqual(result["error_code"], "OPENED_NOT_RETURNABLE")

    def test_opened_earbuds_defective_are_eligible(self):
        order = make_order(
            items=[item(returnable="unopened_only", opened=1, name="Test Buds")]
        )
        result = rules.evaluate_return(order, ["SS-1-1"], "defective", params())
        self.assertTrue(result["eligible"], result)

    def test_gift_card_never_returnable(self):
        order = make_order(items=[item(returnable="no", name="Gift Card")])
        result = rules.evaluate_return(order, ["SS-1-1"], "changed_mind", params())
        self.assertEqual(result["error_code"], "NON_RETURNABLE")

    def test_not_delivered(self):
        order = make_order(status="shipped")
        result = rules.evaluate_return(order, ["SS-1-1"], "changed_mind", params())
        self.assertEqual(result["error_code"], "NOT_DELIVERED")

    def test_invalid_item(self):
        order = make_order()
        result = rules.evaluate_return(order, ["SS-1-9"], "changed_mind", params())
        self.assertEqual(result["error_code"], "INVALID_ITEM")

    def test_invalid_reason(self):
        order = make_order()
        result = rules.evaluate_return(order, ["SS-1-1"], "because", params())
        self.assertEqual(result["error_code"], "INVALID_REASON")


class WarrantyRuleTests(unittest.TestCase):
    def test_month_end(self):
        self.assertEqual(
            rules.warranty_end_date(date(2026, 1, 31), 1), date(2026, 2, 28)
        )
        self.assertEqual(
            rules.warranty_end_date(date(2024, 1, 31), 1), date(2024, 2, 29)
        )

    def test_end_date_is_covered(self):
        end = rules.warranty_end_date(date(2026, 3, 15), 12)
        self.assertEqual(end, date(2027, 3, 15))

    def test_expired_warranty(self):
        order = make_order(
            delivery="2025-01-01",
            items=[
                item(
                    warranty_months=12,
                    warranty_end="2026-01-01",
                    warranty_active=False,
                )
            ],
        )
        result = rules.evaluate_warranty(order, None, params())
        self.assertEqual(result["error_code"], "WARRANTY_EXPIRED")

    def test_no_warranty(self):
        order = make_order(
            items=[item(warranty_class="none", warranty_months=0, warranty_active=False)]
        )
        result = rules.evaluate_warranty(order, None, params())
        self.assertEqual(result["error_code"], "NO_WARRANTY")

    def test_one_covered_item_is_enough(self):
        order = make_order(
            items=[
                item(item_id="SS-1-1", name="Old", warranty_active=False),
                item(item_id="SS-1-2", name="New", warranty_active=True),
            ]
        )
        result = rules.evaluate_warranty(order, None, params())
        self.assertTrue(result["eligible"])
        self.assertEqual([entry["item_id"] for entry in result["covered"]], ["SS-1-2"])


class CancellationRuleTests(unittest.TestCase):
    def test_processing_can_be_cancelled(self):
        self.assertTrue(rules.evaluate_cancellation({"status": "processing"})["eligible"])

    def test_shipped_cannot_be_cancelled(self):
        result = rules.evaluate_cancellation({"status": "shipped"})
        self.assertEqual(result["error_code"], "ORDER_ALREADY_SHIPPED")

    def test_delivered_cannot_be_cancelled(self):
        result = rules.evaluate_cancellation({"status": "delivered"})
        self.assertEqual(result["error_code"], "ORDER_ALREADY_SHIPPED")


if __name__ == "__main__":
    unittest.main()
