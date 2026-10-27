"""Policy parsing, manifest filtering and retrieval tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.services.policy_search import PolicyIndex

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


@pytest.fixture(scope="module")
def index() -> PolicyIndex:
    return PolicyIndex(
        DATA / "policies.md",
        DATA / "manifest.json",
        date(2026, 7, 1),
    )


def sections(results) -> list[tuple[str, str]]:
    return [(item.chunk.doc_id, item.chunk.section) for item in results]


def test_all_ten_documents_are_parsed(index: PolicyIndex):
    doc_ids = {chunk.doc_id for chunk in index.chunks}
    assert len(doc_ids) == 10
    assert "returns_policy" in doc_ids
    assert "returns_policy_2024" in doc_ids
    assert len(index) > 40


def test_active_documents_exclude_superseded_and_future(index: PolicyIndex):
    active = index.active_doc_ids()
    assert "returns_policy" in active
    assert "shipping_policy" in active
    assert "returns_policy_2024" not in active
    assert "shipping_policy_update" not in active


def test_return_window_search_uses_current_returns_policy(index: PolicyIndex):
    results = index.search("return window days", limit=5)
    assert results
    assert (results[0].chunk.doc_id, results[0].chunk.section) == (
        "returns_policy",
        "§2.1",
    )
    assert all(item.chunk.doc_id != "returns_policy_2024" for item in results)


def test_superseded_document_is_never_returned(index: PolicyIndex):
    for query in (
        "2024 returns policy 14 days",
        "old return policy",
        "return within 14 days",
    ):
        assert all(
            item.chunk.doc_id != "returns_policy_2024"
            for item in index.search(query, limit=10)
        )


def test_future_document_is_only_available_as_future(index: PolicyIndex):
    assert all(
        item.chunk.doc_id != "shipping_policy_update"
        for item in index.search("shipping fee", limit=10)
    )
    future = index.search_future("shipping fee", limit=3)
    assert future
    assert all(item.chunk.doc_id == "shipping_policy_update" for item in future)
    assert future[0].chunk.effective_date == date(2026, 8, 1)


def test_policy_sections_are_citable(index: PolicyIndex):
    results = index.search("opened earbuds hygiene seal", limit=5)
    assert ("returns_policy", "§4.2") in sections(results)
    assert all(item.chunk.section.startswith("§") for item in results)


def test_policy_is_preferred_over_faq_when_both_match(index: PolicyIndex):
    results = index.search("ShopSphere Plus membership", limit=5)
    assert results
    assert results[0].chunk.doc_id == "shipping_policy"


def test_uncovered_topics_return_nothing(index: PolicyIndex):
    for query in (
        "gift wrapping",
        "price match",
        "do you sell phones",
        "what is the capital of France",
    ):
        assert index.search(query, limit=5) == []


def test_refund_and_cancellation_retrieval(index: PolicyIndex):
    refunds = index.search("refund processing business days", limit=5)
    assert ("refunds_policy", "§2.1") in sections(refunds)

    cancellation = index.search("cancel order before shipment", limit=5)
    assert ("order_cancellation", "§1.1") in sections(cancellation)
