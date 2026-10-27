"""Tests for the optional LLM tool loop using a scripted complete_fn."""

from __future__ import annotations

from app.services.agent import LLMReply

from tests.helpers import count_rows, json_chat


class FakeLLM:
    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    def __call__(self, settings, messages, tools, timeout_s=20.0):
        self.seen.append(messages)
        if not self.steps:
            raise AssertionError("The fake LLM was called more times than expected")
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def install(api, fake: FakeLLM) -> FakeLLM:
    api.app.state.assistant.complete_fn = fake
    return fake


def test_agent_reads_a_tool_then_answers(agent_api):
    fake = install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "get_orders",
                            "arguments": {"order_id": "SS-10512"},
                        }
                    ]
                ),
                LLMReply(
                    text=(
                        '{"answer": "Order SS-10512 is in transit and due on 3 July '
                        '2026.", "route": "answer", "citations": []}'
                    )
                ),
            ]
        ),
    )
    body = json_chat(
        agent_api.client, "C1009", "agent-read", "Where is order SS-10512?"
    )
    assert body["route"] == "answer"
    assert "3 July 2026" in body["answer"]
    assert body["tool_calls"] == [
        {
            "name": "get_orders",
            "args": {"order_id": "SS-10512"},
            "status": "success",
        }
    ]
    assert len(fake.seen) == 2


def test_agent_policy_citations_must_come_from_search_results(agent_api):
    fake = install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "search_policy",
                            "arguments": {"query": "return window days"},
                        }
                    ]
                ),
                LLMReply(
                    text=(
                        '{"answer": "You have 30 days.", "route": "answer", '
                        '"citations": [{"doc_id": "returns_policy", "section": '
                        '"§2.1"}, {"doc_id": "made_up", "section": "§9.9"}]}'
                    )
                ),
            ]
        ),
    )
    body = json_chat(
        agent_api.client, "C1009", "agent-policy", "How many days do I have?"
    )
    assert body["route"] == "answer"
    assert body["citations"] == [{"doc_id": "returns_policy", "section": "§2.1"}]
    assert len(fake.seen) == 2


def test_agent_write_is_intercepted_as_a_proposal(agent_api):
    before = count_rows(agent_api.db_path, "return_requests")
    install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "create_return_request",
                            "arguments": {
                                "order_id": "SS-10458",
                                "item_ids": ["SS-10458-1"],
                                "reason": "changed_mind",
                            },
                        }
                    ]
                )
            ]
        ),
    )
    proposal = json_chat(
        agent_api.client,
        "C1009",
        "agent-write",
        "Please return the speaker from SS-10458, I changed my mind.",
    )
    assert proposal["pending_confirmation"] is True
    assert proposal["route"] == "answer"
    assert proposal["tool_calls"] == [
        {
            "name": "create_return_request",
            "args": {
                "order_id": "SS-10458",
                "item_ids": ["SS-10458-1"],
                "reason": "changed_mind",
            },
            "status": "pending_confirmation",
        }
    ]
    assert count_rows(agent_api.db_path, "return_requests") == before

    # Confirmation is always handled by the deterministic planner, never by
    # the model.
    def boom(*args, **kwargs):
        raise AssertionError("The LLM must not run on a confirmation turn")

    agent_api.app.state.assistant.complete_fn = boom
    confirmed = json_chat(agent_api.client, "C1009", "agent-write", "yes")
    assert confirmed["pending_confirmation"] is False
    assert confirmed["tool_calls"][-1]["status"] == "success"
    assert count_rows(agent_api.db_path, "return_requests") == before + 1


def test_agent_cannot_write_for_a_pure_question(agent_api):
    before = count_rows(agent_api.db_path, "return_requests")
    install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "create_return_request",
                            "arguments": {
                                "order_id": "SS-10458",
                                "item_ids": ["SS-10458-1"],
                                "reason": "changed_mind",
                            },
                        }
                    ]
                ),
                LLMReply(
                    text=(
                        '{"answer": "Yes, it is within the 30-day window. I can '
                        'create the request if you want.", "route": "answer", '
                        '"citations": []}'
                    )
                ),
            ]
        ),
    )
    body = json_chat(
        agent_api.client,
        "C1009",
        "agent-question",
        "Can I return the speaker from SS-10458? I changed my mind.",
    )
    assert body["route"] == "answer"
    assert body["pending_confirmation"] is False
    assert count_rows(agent_api.db_path, "return_requests") == before


def test_agent_cannot_write_for_another_customers_order(agent_api):
    before = count_rows(agent_api.db_path, "return_requests")
    install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "create_return_request",
                            "arguments": {
                                "order_id": "SS-10458",
                                "item_ids": ["SS-10458-1"],
                                "reason": "changed_mind",
                            },
                        }
                    ]
                )
            ]
        ),
    )
    body = json_chat(
        agent_api.client,
        "C1010",
        "agent-foreign",
        "Please return the speaker from SS-10458, I changed my mind.",
    )
    assert body["route"] == "no_answer"
    assert body["answer"] == "I couldn't find that order on your account."
    assert count_rows(agent_api.db_path, "return_requests") == before


def test_agent_missing_order_wins_over_the_final_text(agent_api):
    install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    tool_calls=[
                        {
                            "id": "call-1",
                            "name": "get_orders",
                            "arguments": {"order_id": "SS-99999"},
                        }
                    ]
                ),
                LLMReply(
                    text=(
                        '{"answer": "Here is your order.", "route": "answer", '
                        '"citations": []}'
                    )
                ),
            ]
        ),
    )
    body = json_chat(
        agent_api.client, "C1009", "agent-missing", "Where is order SS-99999?"
    )
    assert body["route"] == "no_answer"
    assert body["answer"] == "I couldn't find that order on your account."
    assert body["citations"] == []


def test_agent_out_of_scope_clears_citations_and_tool_calls(agent_api):
    install(
        agent_api,
        FakeLLM(
            [
                LLMReply(
                    text=(
                        '{"answer": "I can only help with ShopSphere orders.", '
                        '"route": "out_of_scope", "citations": '
                        '[{"doc_id": "returns_policy", "section": "§2.1"}]}'
                    )
                )
            ]
        ),
    )
    body = json_chat(
        agent_api.client, "C1009", "agent-off-topic", "Where is order SS-10458?"
    )
    assert body["route"] == "out_of_scope"
    assert body["citations"] == []
    assert body["tool_calls"] == []


def test_agent_failure_falls_back_to_the_deterministic_planner(agent_api):
    before = count_rows(agent_api.db_path, "return_requests")
    install(agent_api, FakeLLM([RuntimeError("provider unavailable")]))
    body = json_chat(
        agent_api.client, "C1009", "agent-fallback", "Where is order SS-10512?"
    )
    assert body["route"] == "answer"
    assert "in transit" in body["answer"].lower()
    assert body["tool_calls"][0]["name"] == "get_orders"
    assert count_rows(agent_api.db_path, "return_requests") == before
