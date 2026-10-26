"""HTTP tests for POST /chat, using a temporary copy of the database.

Run:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DB = ROOT / "data" / "shopsphere.db"


class ChatApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.tmp.name) / "shopsphere.db"
        shutil.copy2(SOURCE_DB, cls.db_path)

        os.environ["DB_PATH"] = str(cls.db_path)
        os.environ["POLICIES_PATH"] = str(ROOT / "data" / "policies.md")
        os.environ["MANIFEST_PATH"] = str(ROOT / "data" / "manifest.json")
        os.environ["APP_TODAY"] = "2026-07-01"
        os.environ["LLM_PROVIDER"] = "none"

        from main import app

        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        os.environ.pop("DB_PATH", None)
        cls.tmp.cleanup()

    # ------------------------------------------------------------- helpers
    def chat(self, message, customer="C1009", session="s"):
        return self.client.post(
            "/chat",
            headers={"X-Customer-Id": customer},
            json={"session_id": session, "message": message},
        )

    def count(self, table):
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    # --------------------------------------------------------------- tests
    def test_health(self):
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)

    def test_track_order(self):
        response = self.chat("Where is order SS-10512?", session="track1")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["route"], "answer")
        self.assertIn("SS-10512", body["answer"])
        self.assertIn("3 July 2026", body["answer"])
        self.assertEqual(body["tool_calls"][0]["name"], "get_orders")

    def test_missing_header(self):
        response = self.client.post(
            "/chat", json={"session_id": "x", "message": "hi"}
        )
        self.assertEqual(response.status_code, 401)

    def test_unknown_customer(self):
        response = self.chat("hi", customer="C9999", session="x")
        self.assertEqual(response.status_code, 401)

    def test_bad_json(self):
        response = self.client.post(
            "/chat",
            headers={
                "X-Customer-Id": "C1009",
                "Content-Type": "application/json",
            },
            content="{not json",
        )
        self.assertEqual(response.status_code, 400)

    def test_missing_message(self):
        response = self.client.post(
            "/chat",
            headers={"X-Customer-Id": "C1009"},
            json={"session_id": "no-message"},
        )
        self.assertEqual(response.status_code, 400)

    def test_session_belongs_to_another_customer(self):
        self.chat("hello", customer="C1009", session="owner")
        response = self.chat("hello", customer="C1002", session="owner")
        self.assertEqual(response.status_code, 403)

    def test_foreign_order_is_hidden(self):
        response = self.chat("Where is order SS-10530?", session="foreign")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["route"], "no_answer")
        self.assertEqual(body["answer"], "I couldn't find that order on your account.")
        self.assertEqual(body["citations"], [])

    def test_policy_question_quotes_section(self):
        response = self.chat(
            "How many days do I have to return something?", session="policy1"
        )
        body = response.json()
        self.assertEqual(body["route"], "answer")
        self.assertIn("returns_policy", [c["doc_id"] for c in body["citations"]])
        self.assertIn("§2.1", [c["section"] for c in body["citations"]])

    def test_return_requires_confirmation_before_saving(self):
        before = self.count("return_requests")
        response = self.chat(
            "I want to return the speaker from SS-10458, I changed my mind.",
            session="return1",
        )
        body = response.json()
        self.assertEqual(body["route"], "answer")
        self.assertTrue(body["pending_confirmation"])
        self.assertEqual(body["tool_calls"][-1]["status"], "pending_confirmation")
        self.assertEqual(self.count("return_requests"), before)

        confirmed = self.chat("Yes, go ahead.", session="return1").json()
        self.assertFalse(confirmed["pending_confirmation"])
        self.assertEqual(confirmed["tool_calls"][-1]["name"], "create_return_request")
        self.assertEqual(confirmed["tool_calls"][-1]["status"], "success")
        self.assertEqual(self.count("return_requests"), before + 1)

        # The proposal is spent: a second "yes" must not save a duplicate.
        self.chat("yes", session="return1")
        self.assertEqual(self.count("return_requests"), before + 1)

    def test_opened_earbuds_changed_mind_refused(self):
        body = self.chat(
            "I want to return the earbuds from SS-10505, I changed my mind.",
            customer="C1011",
            session="earbuds1",
        ).json()
        self.assertFalse(body["pending_confirmation"])
        self.assertIn("opened", body["answer"].lower())

    def test_opened_earbuds_defective_allowed(self):
        body = self.chat(
            "Please return the earbuds from SS-10505, they are defective.",
            customer="C1011",
            session="earbuds2",
        ).json()
        self.assertTrue(body["pending_confirmation"])

    def test_gift_card_refused(self):
        body = self.chat(
            "Please return the gift card from SS-10476, I changed my mind.",
            customer="C1042",
            session="gift1",
        ).json()
        self.assertFalse(body["pending_confirmation"])

    def test_cancellation_requires_confirmation(self):
        before = self.count("support_tickets")
        body = self.chat(
            "Please cancel order SS-10530.", customer="C1039", session="cancel1"
        ).json()
        self.assertTrue(body["pending_confirmation"])
        self.assertEqual(self.count("support_tickets"), before)

        confirmed = self.chat("yes", customer="C1039", session="cancel1").json()
        self.assertEqual(confirmed["tool_calls"][-1]["status"], "success")
        self.assertEqual(self.count("support_tickets"), before + 1)

    def test_shipped_order_cannot_be_cancelled(self):
        body = self.chat(
            "Please cancel order SS-10512.", session="cancel2"
        ).json()
        self.assertFalse(body["pending_confirmation"])
        self.assertIn("shipped", body["answer"].lower())

    def test_out_of_scope(self):
        body = self.chat("Tell me a joke.", session="off1").json()
        self.assertEqual(body["route"], "out_of_scope")
        self.assertEqual(body["citations"], [])
        self.assertEqual(body["tool_calls"], [])


if __name__ == "__main__":
    unittest.main()
