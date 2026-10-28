"""HTTP contract: response shape, routes and the required HTTP error codes."""

from __future__ import annotations

from tests.helpers import post_chat

ALLOWED_ROUTES = {"answer", "clarify", "no_answer", "out_of_scope"}
ALLOWED_TOOLS = {
    "get_orders",
    "search_policy",
    "check_return_eligibility",
    "create_return_request",
    "create_support_ticket",
}
ALLOWED_STATUSES = {"success", "error", "pending_confirmation"}


def test_health(api):
    response = api.client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["app_today"] == "2026-07-01"
    assert body["policy_sections"] > 40


def test_root_redirects_to_api_docs(api):
    response = api.client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs/"

    docs = api.client.get("/docs/")
    assert docs.status_code == 200
    assert "ResolveBot" in docs.text


def test_missing_customer_header_is_401(api):
    response = api.client.post(
        "/chat", json={"session_id": "s", "message": "Where is my order?"}
    )
    assert response.status_code == 401
    assert "X-Customer-Id" in response.json()["detail"]


def test_unknown_customer_is_401(api):
    response = post_chat(api.client, "C9999", "s", "Where is my order?")
    assert response.status_code == 401


def test_invalid_json_is_400(api):
    response = api.client.post(
        "/chat",
        headers={
            "X-Customer-Id": "C1009",
            "Content-Type": "application/json",
        },
        content=b'{"session_id": "s", "message": ',
    )
    assert response.status_code == 400


def test_missing_or_empty_fields_are_400(api):
    cases = [
        {},
        {"session_id": "s"},
        {"message": "Where is my order?"},
        {"session_id": "", "message": "Where is my order?"},
        {"session_id": "s", "message": "   "},
        {"session_id": "s", "message": 123},
        {"session_id": "s", "message": "hello", "extra": True},
    ]
    for body in cases:
        response = api.client.post(
            "/chat", headers={"X-Customer-Id": "C1009"}, json=body
        )
        assert response.status_code == 400, (body, response.text)


def test_session_belongs_to_one_customer(api):
    first = post_chat(api.client, "C1009", "shared-session", "Where is SS-10512?")
    assert first.status_code == 200

    conflict = post_chat(api.client, "C1010", "shared-session", "Where is SS-10458?")
    assert conflict.status_code == 403
    assert "another customer" in conflict.json()["detail"]

    # The original owner can keep using the session.
    again = post_chat(api.client, "C1009", "shared-session", "Where is SS-10458?")
    assert again.status_code == 200


def test_reply_shape_and_unique_request_ids(api):
    first = post_chat(api.client, "C1009", "s1", "Where is order SS-10512?")
    second = post_chat(api.client, "C1009", "s1", "Where is order SS-10458?")
    assert first.status_code == second.status_code == 200
    one, two = first.json(), second.json()

    for body in (one, two):
        assert set(
            {
                "request_id",
                "answer",
                "route",
                "citations",
                "tool_calls",
                "pending_confirmation",
                "session_id",
            }
        ).issubset(body)
        assert body["route"] in ALLOWED_ROUTES
        assert isinstance(body["answer"], str) and body["answer"]
        assert isinstance(body["pending_confirmation"], bool)
        assert isinstance(body["citations"], list)
        for citation in body["citations"]:
            assert set(citation) == {"doc_id", "section"}
        for call in body["tool_calls"]:
            assert call["name"] in ALLOWED_TOOLS
            assert call["status"] in ALLOWED_STATUSES
            assert isinstance(call["args"], dict)
    assert one["request_id"] != two["request_id"]


def test_tracking_answer(api):
    body = post_chat(api.client, "C1009", "s", "Where is order SS-10512?").json()
    assert body["route"] == "answer"
    assert "in transit" in body["answer"].lower()
    assert "3 July 2026" in body["answer"]
    assert body["citations"] == []
    assert body["pending_confirmation"] is False
    assert body["tool_calls"][0]["name"] == "get_orders"
    assert body["tool_calls"][0]["status"] == "success"


def test_unknown_order_uses_exact_reply_and_no_answer(api):
    body = post_chat(api.client, "C1009", "s", "Where is order SS-99999?").json()
    assert body["answer"] == "I couldn't find that order on your account."
    assert body["route"] == "no_answer"
    assert body["citations"] == []
    assert body["pending_confirmation"] is False
    assert body["tool_calls"][0]["status"] == "error"
    assert body["tool_calls"][0]["error_code"] == "ORDER_NOT_FOUND"


def test_foreign_order_is_indistinguishable_from_missing(api):
    missing = post_chat(api.client, "C1009", "a", "Where is order SS-99999?").json()
    foreign = post_chat(api.client, "C1010", "b", "Where is order SS-10458?").json()
    assert foreign["answer"] == missing["answer"]
    assert foreign["route"] == missing["route"] == "no_answer"
    assert foreign["citations"] == []
    assert foreign["tool_calls"][0]["error_code"] == "ORDER_NOT_FOUND"


def test_out_of_scope_has_no_citations_or_tool_calls(api):
    body = post_chat(
        api.client, "C1009", "s", "What's the weather in Mumbai?"
    ).json()
    assert body["route"] == "out_of_scope"
    assert body["citations"] == []
    assert body["tool_calls"] == []
    assert body["pending_confirmation"] is False


def test_business_errors_are_still_http_200(api):
    response = post_chat(
        api.client,
        "C1042",
        "s",
        "Please return the gift card from SS-10476, I changed my mind.",
    )
    assert response.status_code == 200
    body = response.json()
    assert body["route"] == "answer"
    assert body["pending_confirmation"] is False
    check = next(
        call for call in body["tool_calls"] if call["name"] == "check_return_eligibility"
    )
    assert check["status"] == "error"
    assert check["error_code"] == "NON_RETURNABLE"
