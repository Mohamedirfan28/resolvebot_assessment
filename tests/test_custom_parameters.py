"""Integration checks that business decisions follow policy_parameters.

The supplied data uses a 30-day return window. This test uses a temporary copy
with different numbers, proving that the rule is not hard-coded.
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


class ConfigurableRulesTests(unittest.TestCase):
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
        os.environ["LLM_AGENT"] = "0"

        from main import app

        cls.client_context = TestClient(app)
        cls.client = cls.client_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        for key in ("DB_PATH", "POLICIES_PATH", "MANIFEST_PATH", "APP_TODAY"):
            os.environ.pop(key, None)
        os.environ["LLM_PROVIDER"] = "none"
        os.environ["LLM_AGENT"] = "0"
        cls.tmp.cleanup()

    def set_window(self, days: int):
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "UPDATE policy_parameters SET value = ? WHERE key = 'return_window_days'",
                (str(days),),
            )
            conn.commit()
        finally:
            conn.close()

    def chat(self, message, session):
        return self.client.post(
            "/chat",
            headers={"X-Customer-Id": "C1009"},
            json={"session_id": session, "message": message},
        ).json()

    def test_return_window_follows_the_database(self):
        # SS-10458 was delivered on 21 June 2026, i.e. 10 days before APP_TODAY.
        self.set_window(12)
        body = self.chat(
            "Please return the speaker from SS-10458, I changed my mind.",
            session="custom-12",
        )
        self.assertTrue(body["pending_confirmation"], body)
        self.assertEqual(body["tool_calls"][1]["status"], "success")

        self.set_window(9)
        body = self.chat(
            "Please return the speaker from SS-10458, I changed my mind.",
            session="custom-9",
        )
        self.assertFalse(body["pending_confirmation"])
        check = next(
            call
            for call in body["tool_calls"]
            if call["name"] == "check_return_eligibility"
        )
        self.assertEqual(check["status"], "error")
        self.assertEqual(check["error_code"], "RETURN_WINDOW_EXPIRED")


if __name__ == "__main__":
    unittest.main()
