"""Conversation-level tests for the propose-confirm flow and business replies."""

from __future__ import annotations

from tests.helpers import count_rows, json_chat, tool_names


def test_return_proposal_writes_nothing_until_yes(api):
    sid = "return-confirm"
    before = count_rows(api.db_path, "return_requests")

    proposal = json_chat(
        api.client,
        "C1009",
        sid,
        "Return the speaker from SS-10458, I changed my mind.",
    )
    assert proposal["route"] == "answer"
    assert proposal["pending_confirmation"] is True
    assert "Shall I go ahead" in proposal["answer"]
    assert any(
        citation["doc_id"] == "returns_policy" for citation in proposal["citations"]
    )
    assert tool_names(proposal) == [
        "get_orders",
        "check_return_eligibility",
        "search_policy",
        "create_return_request",
    ]
    assert proposal["tool_calls"][-1]["status"] == "pending_confirmation"
    assert count_rows(api.db_path, "return_requests") == before

    confirmed = json_chat(api.client, "C1009", sid, "Yes, go ahead.")
    assert confirmed["route"] == "answer"
    assert confirmed["pending_confirmation"] is False
    assert confirmed["tool_calls"] == [
        {
            "name": "create_return_request",
            "args": {
                "order_id": "SS-10458",
                "item_ids": ["SS-10458-1"],
                "reason": "changed_mind",
            },
            "status": "success",
        }
    ]
    assert count_rows(api.db_path, "return_requests") == before + 1

    # A second "yes" cannot duplicate the write.
    again = json_chat(api.client, "C1009", sid, "yes")
    assert again["route"] == "out_of_scope"
    assert count_rows(api.db_path, "return_requests") == before + 1


def test_no_does_not_write(api):
    sid = "return-no"
    before = count_rows(api.db_path, "return_requests")
    proposal = json_chat(
        api.client,
        "C1009",
        sid,
        "Return the speaker from SS-10458, I changed my mind.",
    )
    assert proposal["pending_confirmation"] is True

    declined = json_chat(api.client, "C1009", sid, "No, not now.")
    assert declined["pending_confirmation"] is False
    assert "haven't created anything" in declined["answer"]
    assert count_rows(api.db_path, "return_requests") == before


def test_off_topic_message_forgets_the_waiting_proposal(api):
    sid = "return-forget"
    before = count_rows(api.db_path, "return_requests")
    proposal = json_chat(
        api.client,
        "C1009",
        sid,
        "Return the speaker from SS-10458, I changed my mind.",
    )
    assert proposal["pending_confirmation"] is True

    off_topic = json_chat(api.client, "C1009", sid, "What's the weather in Mumbai?")
    assert off_topic["route"] == "out_of_scope"
    assert off_topic["pending_confirmation"] is False

    # This "yes" answers nothing: the proposal was abandoned by the previous
    # message and must not write a return request.
    later_yes = json_chat(api.client, "C1009", sid, "yes")
    assert later_yes["route"] == "out_of_scope"
    assert count_rows(api.db_path, "return_requests") == before


def test_any_other_message_forgets_the_waiting_proposal(api):
    sid = "return-forget-tracking"
    before = count_rows(api.db_path, "return_requests")
    json_chat(
        api.client,
        "C1009",
        sid,
        "Return the speaker from SS-10458, I changed my mind.",
    )
    unrelated = json_chat(api.client, "C1009", sid, "Where is order SS-10512?")
    assert unrelated["route"] == "answer"
    assert "in transit" in unrelated["answer"].lower()

    later_yes = json_chat(api.client, "C1009", sid, "yes")
    assert later_yes["route"] == "out_of_scope"
    assert count_rows(api.db_path, "return_requests") == before


def test_just_do_it_alone_is_not_a_yes(api):
    sid = "return-just-do-it"
    before = count_rows(api.db_path, "return_requests")
    json_chat(
        api.client,
        "C1009",
        sid,
        "Return the speaker from SS-10458, I changed my mind.",
    )
    body = json_chat(api.client, "C1009", sid, "just do it")
    assert body["pending_confirmation"] is False
    assert count_rows(api.db_path, "return_requests") == before


def test_multi_order_clarification_then_specific_item(api):
    sid = "charger-clarify"
    before = count_rows(api.db_path, "return_requests")

    first = json_chat(api.client, "C1024", sid, "I want to return my charger")
    assert first["route"] == "clarify"
    for order_id in ("SS-10471", "SS-10489", "SS-10503"):
        assert order_id in first["answer"]
    assert first["pending_confirmation"] is False
    assert count_rows(api.db_path, "return_requests") == before

    second = json_chat(
        api.client, "C1024", sid, "the 65W one, I changed my mind"
    )
    assert second["route"] == "answer"
    assert second["pending_confirmation"] is True
    pending = second["tool_calls"][-1]
    assert pending["name"] == "create_return_request"
    assert pending["args"]["order_id"] == "SS-10471"
    assert pending["args"]["item_ids"] == ["SS-10471-1"]
    assert count_rows(api.db_path, "return_requests") == before

    confirmed = json_chat(api.client, "C1024", sid, "yes")
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(api.db_path, "return_requests") == before + 1


def test_tracking_question_after_clarification_is_not_a_return_answer(api):
    sid = "clarify-then-track"
    before = count_rows(api.db_path, "return_requests")

    first = json_chat(api.client, "C1024", sid, "I want to return my charger")
    assert first["route"] == "clarify"

    tracking = json_chat(api.client, "C1024", sid, "Where is order SS-10471?")
    assert tracking["route"] == "answer"
    assert tracking["pending_confirmation"] is False
    assert "ss-10471" in tracking["answer"].lower()
    assert tracking["tool_calls"][0]["name"] == "get_orders"

    later_yes = json_chat(api.client, "C1024", sid, "yes")
    assert later_yes["route"] == "out_of_scope"
    assert count_rows(api.db_path, "return_requests") == before


def test_missing_reason_is_asked_for_before_proposing(api):
    sid = "missing-reason"
    first = json_chat(
        api.client, "C1009", sid, "Return the speaker from SS-10458."
    )
    assert first["route"] == "clarify"
    assert "change of mind" in first["answer"].lower()
    assert first["pending_confirmation"] is False

    second = json_chat(api.client, "C1009", sid, "I changed my mind.")
    assert second["route"] == "answer"
    assert second["pending_confirmation"] is True
    assert second["tool_calls"][-1]["args"]["reason"] == "changed_mind"


def test_opened_earbuds_changed_mind_vs_defective(api):
    sid = "earbuds"
    changed = json_chat(
        api.client,
        "C1011",
        sid,
        "I want to return the earbuds from SS-10505, I changed my mind.",
    )
    assert changed["route"] == "answer"
    assert changed["pending_confirmation"] is False
    check = next(
        call
        for call in changed["tool_calls"]
        if call["name"] == "check_return_eligibility"
    )
    assert check["status"] == "error"
    assert check["error_code"] == "OPENED_NOT_RETURNABLE"
    assert "opened" in changed["answer"].lower()
    assert count_rows(api.db_path, "return_requests") == 0

    defective = json_chat(
        api.client,
        "C1011",
        "earbuds-defective",
        "I want to return the earbuds from SS-10505, it is defective.",
    )
    assert defective["pending_confirmation"] is True
    assert defective["tool_calls"][-1]["args"]["reason"] == "defective"
    confirmed = json_chat(api.client, "C1011", "earbuds-defective", "yes")
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(api.db_path, "return_requests") == 1


def test_gift_card_is_never_returnable(api):
    body = json_chat(
        api.client,
        "C1042",
        "gift-card",
        "Please return the gift card from SS-10476, I changed my mind.",
    )
    assert body["route"] == "answer"
    assert body["pending_confirmation"] is False
    check = next(
        call for call in body["tool_calls"] if call["name"] == "check_return_eligibility"
    )
    assert check["error_code"] == "NON_RETURNABLE"
    assert "cannot be returned" in body["answer"]
    assert count_rows(api.db_path, "return_requests") == 0


def test_not_delivered_order_cannot_be_returned(api):
    body = json_chat(
        api.client,
        "C1009",
        "not-delivered",
        "Return the mouse from SS-10512, I changed my mind.",
    )
    assert body["route"] == "answer"
    assert body["pending_confirmation"] is False
    check = next(
        call for call in body["tool_calls"] if call["name"] == "check_return_eligibility"
    )
    assert check["error_code"] == "NOT_DELIVERED"
    assert "not been delivered" in body["answer"]


def test_return_window_uses_database_parameters_and_includes_day_30(api, set_parameter):
    set_parameter(api.db_path, "return_window_days", "10")
    eligible = json_chat(
        api.client,
        "C1009",
        "window-10",
        "Return the speaker from SS-10458, I changed my mind.",
    )
    assert eligible["pending_confirmation"] is True
    assert "10-day return window" in eligible["answer"]

    set_parameter(api.db_path, "return_window_days", "9")
    expired = json_chat(
        api.client,
        "C1009",
        "window-9",
        "Return the speaker from SS-10458, I changed my mind.",
    )
    assert expired["pending_confirmation"] is False
    check = next(
        call for call in expired["tool_calls"] if call["name"] == "check_return_eligibility"
    )
    assert check["error_code"] == "RETURN_WINDOW_EXPIRED"
    assert "9-day return window" in expired["answer"]
    assert count_rows(api.db_path, "return_requests") == 0


def test_cancellation_only_while_processing(api):
    before = count_rows(api.db_path, "support_tickets")
    proposal = json_chat(
        api.client, "C1039", "cancel-processing", "Please cancel order SS-10530."
    )
    assert proposal["route"] == "answer"
    assert proposal["pending_confirmation"] is True
    assert proposal["tool_calls"][-1]["name"] == "create_support_ticket"
    assert proposal["tool_calls"][-1]["status"] == "pending_confirmation"
    assert count_rows(api.db_path, "support_tickets") == before

    confirmed = json_chat(api.client, "C1039", "cancel-processing", "yes")
    assert confirmed["pending_confirmation"] is False
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(api.db_path, "support_tickets") == before + 1

    shipped = json_chat(
        api.client, "C1009", "cancel-shipped", "Cancel order SS-10512"
    )
    assert shipped["route"] == "answer"
    assert shipped["pending_confirmation"] is False
    create = next(
        call
        for call in shipped["tool_calls"]
        if call["name"] == "create_support_ticket"
    )
    assert create["status"] == "error"
    assert create["error_code"] == "ORDER_ALREADY_SHIPPED"
    assert count_rows(api.db_path, "support_tickets") == before + 1


def test_warranty_cover_expiry_and_ticket(api):
    covered = json_chat(
        api.client,
        "C1018",
        "warranty-covered",
        "Is my laptop from SS-09871 still under warranty?",
    )
    assert covered["route"] == "answer"
    assert covered["pending_confirmation"] is False
    assert "24-month warranty" in covered["answer"]
    assert "2 January 2027" in covered["answer"]
    assert any(
        citation["doc_id"] == "warranty_policy" for citation in covered["citations"]
    )
    assert count_rows(api.db_path, "support_tickets") == 0

    proposal = json_chat(
        api.client,
        "C1018",
        "warranty-ticket",
        "Please open a warranty ticket for SS-09871, it stopped charging.",
    )
    assert proposal["pending_confirmation"] is True
    assert proposal["tool_calls"][-1]["name"] == "create_support_ticket"
    assert proposal["tool_calls"][-1]["args"]["ticket_type"] == "warranty"
    confirmed = json_chat(api.client, "C1018", "warranty-ticket", "yes")
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(api.db_path, "support_tickets") == 1

    expired = json_chat(
        api.client,
        "C1017",
        "warranty-expired",
        "Is my phone from SS-09934 under warranty?",
    )
    assert expired["pending_confirmation"] is False
    assert "no longer covered" in expired["answer"]

    none = json_chat(
        api.client,
        "C1025",
        "warranty-none",
        "Is my software from SS-10106 under warranty?",
    )
    assert none["pending_confirmation"] is False
    assert "does not have a warranty" in none["answer"]


def test_one_message_can_track_and_answer_a_policy_question(api):
    body = json_chat(
        api.client,
        "C1009",
        "track-and-policy",
        "Where is order SS-10512 and how many days do I have to return something?",
    )
    assert body["route"] == "answer"
    assert "in transit" in body["answer"].lower()
    assert "30 days" in body["answer"]
    assert ("get_orders" in tool_names(body)) and ("search_policy" in tool_names(body))
    assert any(
        citation == {"doc_id": "returns_policy", "section": "§2.1"}
        for citation in body["citations"]
    )
    assert body["pending_confirmation"] is False


def test_bare_order_id_and_greeting_with_store_context(api):
    bare = json_chat(api.client, "C1009", "bare-id", "SS-10458")
    assert bare["route"] == "answer"
    assert "delivered" in bare["answer"].lower()
    assert tool_names(bare) == ["get_orders"]

    greeting = json_chat(
        api.client, "C1009", "greeting-store", "hello, where is my order?"
    )
    assert greeting["route"] == "clarify"
    assert "which one" in greeting["answer"].lower()

    hello = json_chat(api.client, "C1009", "greeting-only", "hello")
    assert hello["route"] == "out_of_scope"


def test_policy_answers_cite_current_documents_and_handle_future_notes(api):
    window = json_chat(
        api.client,
        "C1009",
        "policy-window",
        "How many days do I have to return something?",
    )
    assert window["route"] == "answer"
    assert "30 days" in window["answer"]
    assert any(
        citation["doc_id"] == "returns_policy" for citation in window["citations"]
    )
    assert all(
        citation["doc_id"] != "returns_policy_2024"
        for citation in window["citations"]
    )

    old = json_chat(
        api.client, "C1009", "policy-2024", "What is the 2024 return policy?"
    )
    assert old["route"] == "answer"
    assert "superseded" in old["answer"]
    assert all(
        citation["doc_id"] != "returns_policy_2024" for citation in old["citations"]
    )

    shipping = json_chat(
        api.client, "C1009", "policy-shipping", "How much does shipping cost?"
    )
    assert shipping["route"] == "answer"
    assert "99" in shipping["answer"]
    assert "1 August 2026" in shipping["answer"]
    assert any(
        citation["doc_id"] == "shipping_policy" for citation in shipping["citations"]
    )

    uncovered = json_chat(
        api.client, "C1009", "policy-uncovered", "Do you offer gift wrapping?"
    )
    assert uncovered["route"] == "no_answer"
    assert uncovered["citations"] == []
    assert "policies don't cover" in uncovered["answer"]


def test_question_only_offers_without_writing(api):
    sid = "question-offer"
    before = count_rows(api.db_path, "return_requests")
    offer = json_chat(
        api.client,
        "C1009",
        sid,
        "Can I return the speaker from SS-10458? I changed my mind.",
    )
    assert offer["route"] == "answer"
    assert offer["pending_confirmation"] is False
    assert "Would you like me to create" in offer["answer"]
    assert all(
        call["name"] != "create_return_request" for call in offer["tool_calls"]
    )
    assert count_rows(api.db_path, "return_requests") == before

    confirmed = json_chat(api.client, "C1009", sid, "yes")
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(api.db_path, "return_requests") == before + 1


def test_question_only_offer_is_forgotten_by_an_unrelated_message(api):
    sid = "offer-forget"
    before = count_rows(api.db_path, "return_requests")
    json_chat(
        api.client,
        "C1009",
        sid,
        "Can I return the speaker from SS-10458? I changed my mind.",
    )
    json_chat(api.client, "C1009", sid, "What's the weather in Mumbai?")
    later = json_chat(api.client, "C1009", sid, "yes")
    assert later["route"] == "out_of_scope"
    assert count_rows(api.db_path, "return_requests") == before
