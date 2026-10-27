"""The five ShopSphere tools.

Tools take the customer identity from the request context, never from model
arguments. An order that belongs to another customer is indistinguishable from
a missing order: both return ``ORDER_NOT_FOUND``.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from app.core.config import Settings
from app.db import repository
from app.services import rules
from app.services.policy_search import PolicyIndex


class ToolLimitExceeded(RuntimeError):
    """Raised when a single message uses more tool calls than allowed."""


class ToolContext:
    """Per-request tool executor and tool-call log."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        index: PolicyIndex,
        customer_id: str,
        max_tool_calls: int = 6,
    ):
        self.conn = conn
        self.settings = settings
        self.index = index
        self.customer_id = customer_id
        self.max_tool_calls = max_tool_calls
        self.today: date = settings.app_today
        self.params = rules.PolicyParameters(
            repository.fetch_policy_parameters(conn)
        )
        self.calls: list[dict] = []

    # --------------------------------------------------------------- logging
    def _log(
        self,
        name: str,
        args: dict,
        status: str = "success",
        error_code: str | None = None,
    ) -> None:
        if len(self.calls) >= self.max_tool_calls:
            raise ToolLimitExceeded
        entry: dict = {"name": name, "args": args, "status": status}
        if error_code:
            entry["error_code"] = error_code
        self.calls.append(entry)

    def _error(self, name: str, args: dict, code: str, message: str) -> dict:
        self._log(name, args, status="error", error_code=code)
        return {"error_code": code, "message": message}

    def log_pending(self, name: str, args: dict) -> None:
        self._log(name, args, status="pending_confirmation")

    def log_error(self, name: str, args: dict, code: str) -> None:
        self._log(name, args, status="error", error_code=code)

    # ---------------------------------------------------------------- tools
    def get_orders(
        self, order_id: str | None = None, status: str | None = None
    ) -> dict:
        args: dict = {}
        if order_id:
            args["order_id"] = order_id
        if status:
            args["status"] = status

        orders = repository.fetch_orders(
            self.conn, self.customer_id, order_id=order_id, status=status
        )
        if order_id and not orders:
            return self._error(
                "get_orders",
                args,
                "ORDER_NOT_FOUND",
                "Order not found on this account",
            )
        enriched = [
            rules.enrich_order(order, self.today, self.params)
            for order in orders
        ]
        self._log("get_orders", args)
        return {"orders": enriched}

    def search_policy(self, query: str, limit: int = 3) -> dict:
        args = {"query": query}
        matches = self.index.search(query, limit=max(1, min(limit, 6)))
        self._log("search_policy", args)
        return {
            "results": [
                {
                    "doc_id": item.chunk.doc_id,
                    "title": item.chunk.title,
                    "section": item.chunk.section,
                    "heading": item.chunk.heading,
                    "text": item.chunk.text,
                    "effective_date": item.chunk.effective_date.isoformat(),
                    "score": item.score,
                }
                for item in matches
            ]
        }

    def check_return_eligibility(
        self, order_id: str, item_ids: list[str], reason: str
    ) -> dict:
        args = {"order_id": order_id, "item_ids": item_ids, "reason": reason}
        order = self._get_order(order_id)
        if order is None:
            return self._error(
                "check_return_eligibility",
                args,
                "ORDER_NOT_FOUND",
                "Order not found on this account",
            )
        result = rules.evaluate_return(order, item_ids, reason, self.params)
        if result.get("eligible"):
            self._log("check_return_eligibility", args)
        else:
            self._log(
                "check_return_eligibility",
                args,
                status="error",
                error_code=result["error_code"],
            )
        return result

    def create_return_request(
        self, order_id: str, item_ids: list[str], reason: str
    ) -> dict:
        args = {"order_id": order_id, "item_ids": item_ids, "reason": reason}
        order = self._get_order(order_id)
        if order is None:
            return self._error(
                "create_return_request",
                args,
                "ORDER_NOT_FOUND",
                "Order not found on this account",
            )
        # Re-check immediately before writing; never trust a model's earlier
        # eligibility claim.
        result = rules.evaluate_return(order, item_ids, reason, self.params)
        if not result.get("eligible"):
            return self._error(
                "create_return_request",
                args,
                result["error_code"],
                result["message"],
            )
        request_id = repository.insert_return_request(
            self.conn, self.customer_id, order_id, item_ids, reason
        )
        self._log("create_return_request", args)
        return {
            "request_id": request_id,
            "order_id": order_id,
            "item_ids": item_ids,
            "reason": reason,
            "status": "requested",
        }

    def create_support_ticket(
        self, order_id: str, ticket_type: str, summary: str
    ) -> dict:
        args = {
            "order_id": order_id,
            "ticket_type": ticket_type,
            "summary": summary,
        }
        if ticket_type not in rules.VALID_TICKET_TYPES:
            return self._error(
                "create_support_ticket",
                args,
                "INVALID_TICKET_TYPE",
                "ticket_type must be warranty or cancellation",
            )
        order = self._get_order(order_id)
        if order is None:
            return self._error(
                "create_support_ticket",
                args,
                "ORDER_NOT_FOUND",
                "Order not found on this account",
            )

        if ticket_type == "cancellation":
            check = rules.evaluate_cancellation(order)
        else:
            check = rules.evaluate_warranty(order, None, self.params)

        if not check.get("eligible"):
            return self._error(
                "create_support_ticket",
                args,
                check["error_code"],
                check["message"],
            )

        ticket_id = repository.insert_support_ticket(
            self.conn, self.customer_id, order_id, ticket_type, summary
        )
        self._log("create_support_ticket", args)
        return {
            "ticket_id": ticket_id,
            "order_id": order_id,
            "ticket_type": ticket_type,
            "status": "open",
        }

    # -------------------------------------------------------------- helpers
    def _get_order(self, order_id: str) -> dict | None:
        orders = repository.fetch_orders(
            self.conn, self.customer_id, order_id=order_id
        )
        if not orders:
            return None
        return rules.enrich_order(orders[0], self.today, self.params)

    def lookup_order(self, order_id: str) -> dict | None:
        """Owner-scoped lookup used by the planner before proposing a write."""
        return self._get_order(order_id)

    def all_orders(self) -> list[dict]:
        orders = repository.fetch_orders(self.conn, self.customer_id)
        return [
            rules.enrich_order(order, self.today, self.params)
            for order in orders
        ]
