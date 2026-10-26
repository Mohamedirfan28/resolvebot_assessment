"""Tests for the optional LLM tool loop.

The provider is replaced by a scripted fake, so these tests need no API key and
no network. They check the safety properties that matter most: the model cannot
write before a yes, cannot see another customer's order, and cannot propose an
ineligible return.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from app import repository
from app.agent import LLMReply
from app.assistant import Assistant
from app.assistant_types import ChatResult
from app.config import Settings
from app.policy_search import PolicyIndex
from app.sessions import Session
from app.tools import ToolContext

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "shopsphere.db"


class ScriptedLLM:
    """Returns scripted replies and remembers the prompts it was given."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, settings, messages, tools, timeout_s=20.0):
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("the loop asked for more replies than scripted")
        return self.replies.pop(0)


class AgentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "shopsphere.db"
        shutil.copy2(SOURCE_DB, cls.db_path)
        cls.index = PolicyIndex(
            ROOT / "data" / "policies.md",
            ROOT / "data" / "manifest.json",
            date(2026, 7, 1),
        )
        cls.settings = Settings(
            policies_path=ROOT / "data" / "policies.md",
            manifest_path=ROOT / "data" / "manifest.json",
            db_path=cls.db_path,
            app_today=date(2026, 7, 1),
            llm_provider="openai",
            llm_model="",
            llm_base_url="",
            llm_polish=False,
            llm_agent=True,
            openai_api_key="test-key",
            anthropic_api_key="",
            google_api_key="",
            host="127.0.0.1",
            port=8000,
            max_tool_calls=6,
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def ctx(self, customer="C1009", max_tool_calls=6):
        return ToolContext(
            repository.connect(self.db_path),
            self.settings,
            self.index,
            customer,
            max_tool_calls,
        )

    def count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def run_chat(self, message, replies, customer="C1009", session_id="s"):
        fake = ScriptedLLM(replies)
        assistant = Assistant(max_tool_calls=6, settings=self.settings, complete_fn=fake)
        ctx = self.ctx(customer)
        session = Session(session_id=session_id, customer_id=customer)
        try:
            result = assistant.handle(ctx, session, message)
        finally:
            ctx.conn.close()
        return result, session, fake, assistant

    # ------------------------------------------------------------- tests
    def test_agent_searches_and_cites_policy(self):
        replies = [
            LLMReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "search_policy",
                        "arguments": {"query": "return window days"},
                    }
                ]
            ),
            LLMReply(
                text=(
                    '{"answer": "You have 30 days from delivery.", "route": "answer", '
                    '"citations": [{"doc_id": "returns_policy", "section": "§2.1"}]}'
                )
            ),
        ]
        result, _, fake, _ = self.run_chat(
            "How many days do I have to return something?", replies
        )
        self.assertEqual(result.route, "answer")
        self.assertIn("30 days", result.answer)
        self.assertEqual(
            result.citations, [{"doc_id": "returns_policy", "section": "§2.1"}]
        )
        self.assertEqual(result.tool_calls[0]["name"], "search_policy")
        self.assertEqual(fake.calls[1][-1]["role"], "tool")

    def test_agent_proposes_return_but_saves_nothing(self):
        before = self.count("return_requests")
        replies = [
            LLMReply(
                tool_calls=[
                    {
                        "id": "c1",
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
        result, session, _, assistant = self.run_chat(
            "Please return the speaker from SS-10458, I changed my mind.", replies
        )
        self.assertTrue(result.pending_confirmation)
        self.assertEqual(result.tool_calls[-1]["name"], "create_return_request")
        self.assertEqual(result.tool_calls[-1]["status"], "pending_confirmation")
        self.assertIn("Shall I go ahead", result.answer)
        self.assertEqual(self.count("return_requests"), before)

        # The next message is a clear yes: the deterministic path executes it.
        yes_ctx = self.ctx()
        try:
            confirmed = assistant.handle(yes_ctx, session, "Yes, go ahead.")
        finally:
            yes_ctx.conn.close()
        self.assertEqual(confirmed.tool_calls[-1]["status"], "success")
        self.assertEqual(self.count("return_requests"), before + 1)
        self.assertIsNone(session.pending)

    def test_agent_does_not_propose_gift_card_return(self):
        before = self.count("return_requests")
        replies = [
            LLMReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "create_return_request",
                        "arguments": {
                            "order_id": "SS-10476",
                            "item_ids": ["SS-10476-1"],
                            "reason": "changed_mind",
                        },
                    }
                ]
            )
        ]
        result, session, _, _ = self.run_chat(
            "Please return the gift card from SS-10476.",
            replies,
            customer="C1042",
        )
        self.assertFalse(result.pending_confirmation)
        self.assertIsNone(session.pending)
        self.assertIn("cannot be returned", result.answer)
        self.assertEqual(self.count("return_requests"), before)

    def test_agent_hides_foreign_order(self):
        replies = [
            LLMReply(
                tool_calls=[
                    {
                        "id": "c1",
                        "name": "create_return_request",
                        "arguments": {
                            "order_id": "SS-10530",
                            "item_ids": ["SS-10530-1"],
                            "reason": "changed_mind",
                        },
                    }
                ]
            )
        ]
        result, session, _, _ = self.run_chat("Return order SS-10530.", replies)
        self.assertEqual(result.route, "no_answer")
        self.assertEqual(result.answer, "I couldn't find that order on your account.")
        self.assertEqual(result.citations, [])
        self.assertIsNone(session.pending)

    def test_agent_falls_back_when_provider_fails(self):
        class Broken:
            def __call__(self, *args, **kwargs):
                raise RuntimeError("provider down")

        assistant = Assistant(
            max_tool_calls=6, settings=self.settings, complete_fn=Broken()
        )
        ctx = self.ctx()
        session = Session(session_id="fallback", customer_id="C1009")
        try:
            result = assistant.handle(ctx, session, "Where is order SS-10512?")
        finally:
            ctx.conn.close()

        self.assertIsInstance(result, ChatResult)
        self.assertIn("SS-10512", result.answer)
        self.assertEqual(result.route, "answer")

    def test_agent_stops_at_tool_limit(self):
        replies = [
            LLMReply(
                tool_calls=[
                    {
                        "id": f"c{i}",
                        "name": "search_policy",
                        "arguments": {"query": "warranty"},
                    }
                ]
            )
            for i in range(10)
        ]
        fake = ScriptedLLM(replies)
        assistant = Assistant(
            max_tool_calls=3, settings=self.settings, complete_fn=fake
        )
        ctx = self.ctx(max_tool_calls=3)
        session = Session(session_id="limit", customer_id="C1009")
        try:
            result = assistant.handle(ctx, session, "Tell me about warranty.")
        finally:
            ctx.conn.close()

        self.assertLessEqual(len(result.tool_calls), 3)
        self.assertIn("limit", result.answer.lower())


if __name__ == "__main__":
    unittest.main()
