"""Unit tests for the safety-critical business rules."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.rules import (
    PolicyParameterError,
    PolicyParameters,
    enrich_order,
    evaluate_cancellation,
    evaluate_return,
    evaluate_warranty,
    warranty_end_date,
    warranty_months,
)


def parameters(window: int = 30, standard: int = 12, extended: int = 24) -> PolicyParameters:
    return PolicyParameters(
        {
            "return_window_days": {"value": str(window)},
            "standard_warranty_months": {"value": str(standard)},
            "extended_warranty_months": {"value": str(extended)},
        }
    )


def item(
    item_id: str = "SS-TEST-1",
    *,
    name: str = "Test Speaker",
    returnable: str = "yes",
    opened: bool = False,
    warranty_class: str = "standard",
) -> dict:
    return {
        "item_id": item_id,
        "name": name,
        "returnable": returnable,
        "opened": opened,
        "warranty_class": warranty_class,
    }


def delivered_order(
    *,
    days: int = 10,
    items: list[dict] | None = None,
    status: str = "delivered",
) -> dict:
    return {
        "order_id": "SS-TEST",
        "status": status,
        "delivery_date": "2026-06-01",
        "days_since_delivery": days,
        "items": items or [item()],
    }


# ------------------------------------------------------------------ returns
def test_return_is_eligible_on_day_30_inclusive():
    result = evaluate_return(
        delivered_order(days=30), ["SS-TEST-1"], "changed_mind", parameters()
    )
    assert result["eligible"] is True
    assert result["days_since_delivery"] == 30


def test_return_is_expired_on_day_31():
    result = evaluate_return(
        delivered_order(days=31), ["SS-TEST-1"], "changed_mind", parameters()
    )
    assert result["eligible"] is False
    assert result["error_code"] == "RETURN_WINDOW_EXPIRED"


def test_return_window_comes_from_parameters_not_code():
    params = parameters(window=7)
    assert evaluate_return(
        delivered_order(days=7), ["SS-TEST-1"], "changed_mind", params
    )["eligible"] is True
    result = evaluate_return(
        delivered_order(days=8), ["SS-TEST-1"], "changed_mind", params
    )
    assert result["error_code"] == "RETURN_WINDOW_EXPIRED"


def test_non_returnable_is_checked_before_the_window():
    order = delivered_order(
        days=500, items=[item(name="Gift Card", returnable="no")]
    )
    result = evaluate_return(order, ["SS-TEST-1"], "changed_mind", parameters())
    assert result["error_code"] == "NON_RETURNABLE"


def test_opened_unopened_only_item_fails_for_change_of_mind():
    order = delivered_order(
        days=5,
        items=[item(name="Auralis Buds Air", returnable="unopened_only", opened=True)],
    )
    result = evaluate_return(order, ["SS-TEST-1"], "changed_mind", parameters())
    assert result["error_code"] == "OPENED_NOT_RETURNABLE"


def test_opened_unopened_only_item_is_eligible_when_defective():
    order = delivered_order(
        days=5,
        items=[item(name="Auralis Buds Air", returnable="unopened_only", opened=True)],
    )
    result = evaluate_return(order, ["SS-TEST-1"], "defective", parameters())
    assert result["eligible"] is True


def test_opened_item_that_is_normally_returnable_still_passes():
    order = delivered_order(days=30, items=[item(opened=True)])
    assert evaluate_return(
        order, ["SS-TEST-1"], "changed_mind", parameters()
    )["eligible"] is True


def test_not_delivered_order_cannot_be_returned():
    result = evaluate_return(
        delivered_order(days=0, status="processing"),
        ["SS-TEST-1"],
        "changed_mind",
        parameters(),
    )
    assert result["error_code"] == "NOT_DELIVERED"


def test_invalid_item_and_reason_are_reported():
    order = delivered_order()
    assert (
        evaluate_return(order, ["SS-TEST-9"], "changed_mind", parameters())["error_code"]
        == "INVALID_ITEM"
    )
    assert (
        evaluate_return(order, [], "changed_mind", parameters())["error_code"]
        == "INVALID_ITEM"
    )
    assert (
        evaluate_return(order, ["SS-TEST-1"], "because", parameters())["error_code"]
        == "INVALID_REASON"
    )
    assert (
        evaluate_return(None, ["SS-TEST-1"], "changed_mind", parameters())["error_code"]
        == "ORDER_NOT_FOUND"
    )


# ----------------------------------------------------------------- warranty
@pytest.mark.parametrize(
    ("delivery", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),  # 2026 is not a leap year
        (date(2024, 1, 31), 1, date(2024, 2, 29)),  # leap year
        (date(2025, 3, 15), 12, date(2026, 3, 15)),
        (date(2025, 1, 2), 24, date(2027, 1, 2)),
    ],
)
def test_warranty_end_handles_month_end(delivery, months, expected):
    assert warranty_end_date(delivery, months) == expected


def test_warranty_class_lengths_come_from_parameters():
    params = parameters(standard=9, extended=36)
    assert warranty_months("standard", params) == 9
    assert warranty_months("extended", params) == 36
    assert warranty_months("none", params) == 0
    assert warranty_months("", params) == 0


def test_warranty_end_date_is_covered_inclusive():
    params = parameters()
    order = {
        "order_id": "SS-TEST",
        "status": "delivered",
        "shipment": {"delivered_at": "2025-07-01T10:00:00"},
        "items": [item(warranty_class="standard")],
    }
    enriched_end_day = enrich_order(order.copy(), date(2026, 7, 1), params)
    assert enriched_end_day["items"][0]["warranty_end"] == "2026-07-01"
    assert enriched_end_day["items"][0]["warranty_active"] is True

    enriched_next_day = enrich_order(
        {**order, "items": [item(warranty_class="standard")]},
        date(2026, 7, 2),
        params,
    )
    assert enriched_next_day["items"][0]["warranty_active"] is False


def test_extended_warranty_is_still_active_after_standard_would_expire():
    params = parameters()
    order = {
        "order_id": "SS-TEST",
        "status": "delivered",
        "shipment": {"delivered_at": "2025-01-02T10:00:00"},
        "items": [item(warranty_class="extended")],
    }
    enriched = enrich_order(order, date(2026, 7, 1), params)
    result = evaluate_warranty(enriched, ["SS-TEST-1"], params)
    assert result["eligible"] is True
    assert result["covered"][0]["warranty_months"] == 24
    assert result["covered"][0]["warranty_end"] == "2027-01-02"


def test_warranty_errors():
    params = parameters()
    expired_order = {
        "order_id": "SS-TEST",
        "status": "delivered",
        "shipment": {"delivered_at": "2025-01-02T10:00:00"},
        "items": [item(warranty_class="standard")],
    }
    expired = enrich_order(expired_order, date(2026, 7, 1), params)
    assert evaluate_warranty(expired, None, params)["error_code"] == "WARRANTY_EXPIRED"

    no_warranty_order = {
        "order_id": "SS-TEST",
        "status": "delivered",
        "shipment": {"delivered_at": "2026-06-01T10:00:00"},
        "items": [item(warranty_class="none")],
    }
    no_warranty = enrich_order(no_warranty_order, date(2026, 7, 1), params)
    assert evaluate_warranty(no_warranty, None, params)["error_code"] == "NO_WARRANTY"

    not_delivered = {
        "order_id": "SS-TEST",
        "status": "processing",
        "items": [item()],
    }
    assert evaluate_warranty(not_delivered, None, params)["error_code"] == "NOT_DELIVERED"
    assert evaluate_warranty(None, None, params)["error_code"] == "ORDER_NOT_FOUND"


def test_warranty_claim_passes_when_at_least_one_item_is_covered():
    params = parameters()
    order = {
        "order_id": "SS-TEST",
        "status": "delivered",
        "shipment": {"delivered_at": "2024-09-01T10:00:00"},
        "items": [
            item("SS-TEST-1", warranty_class="standard"),   # expired 2025-09-01
            item("SS-TEST-2", warranty_class="extended"),   # active until 2026-09-01
            item("SS-TEST-3", warranty_class="none"),
        ],
    }
    enriched = enrich_order(order, date(2026, 7, 1), params)
    result = evaluate_warranty(enriched, None, params)
    assert result["eligible"] is True
    assert [entry["item_id"] for entry in result["covered"]] == ["SS-TEST-2"]
    assert [entry["item_id"] for entry in result["expired"]] == ["SS-TEST-1"]
    assert [entry["item_id"] for entry in result["no_warranty"]] == ["SS-TEST-3"]

    only_expired = evaluate_warranty(enriched, ["SS-TEST-1"], params)
    assert only_expired["error_code"] == "WARRANTY_EXPIRED"
    missing = evaluate_warranty(enriched, ["SS-TEST-9"], params)
    assert missing["error_code"] == "INVALID_ITEM"


# ------------------------------------------------------------- cancellation
def test_cancellation_only_while_processing():
    assert evaluate_cancellation({"status": "processing"})["eligible"] is True
    assert (
        evaluate_cancellation({"status": "shipped"})["error_code"]
        == "ORDER_ALREADY_SHIPPED"
    )
    assert (
        evaluate_cancellation({"status": "delivered"})["error_code"]
        == "ORDER_ALREADY_SHIPPED"
    )
    assert (
        evaluate_cancellation({"status": "cancelled"})["error_code"]
        == "ORDER_ALREADY_SHIPPED"
    )
    assert evaluate_cancellation(None)["error_code"] == "ORDER_NOT_FOUND"


# ------------------------------------------------------------------- params
def test_missing_business_number_fails_loudly():
    with pytest.raises(PolicyParameterError):
        PolicyParameters({}).require_int("return_window_days")
    with pytest.raises(PolicyParameterError):
        PolicyParameters({"return_window_days": {"value": "not a number"}}).require_int(
            "return_window_days"
        )
