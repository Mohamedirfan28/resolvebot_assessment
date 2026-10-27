"""The conversation planner.

The model does not decide business outcomes: this module calls the tools and
applies the rules, then writes the reply. It also owns the propose-then-confirm
state machine and the clarification behaviour.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import nlu
from . import agent
from .assistant_types import ChatResult
from .sessions import Proposal, Session
from .tools import ToolContext, ToolLimitExceeded
from .util import format_date, parse_date, trim_sentences

logger = logging.getLogger("resolvebot.assistant")

NOT_FOUND_ANSWER = "I couldn't find that order on your account."
SUPPORT_EMAIL = "support@shopsphere.example.com"

STATUS_WORDS = {
    "processing": "being prepared and has not shipped yet",
    "shipped": "with the carrier",
    "in_transit": "in transit",
    "out_for_delivery": "out for delivery",
    "delayed": "delayed",
    "delivered": "delivered",
}


@dataclass
class Resolution:
    order: dict | None = None
    items: list[dict] = field(default_factory=list)
    clarify: str | None = None
    not_found: bool = False
    no_orders: bool = False
    bad_items: list[str] = field(default_factory=list)


class Assistant:
    def __init__(self, max_tool_calls: int = 6, settings=None, complete_fn=None):
        self.max_tool_calls = max_tool_calls
        self.settings = settings
        self.complete_fn = complete_fn

    # ================================================================ public
    def handle(self, ctx: ToolContext, session: Session, message: str) -> ChatResult:
        session.remember("user", message)
        try:
            if self._agent_enabled(session):
                try:
                    result = agent.run(
                        self.settings,
                        ctx,
                        session,
                        message,
                        complete_fn=self.complete_fn,
                    )
                    session.clarification = None
                except ToolLimitExceeded:
                    raise
                except Exception:
                    logger.exception(
                        "LLM agent failed; falling back to the deterministic planner"
                    )
                    ctx.calls.clear()
                    session.pending = None
                    result = self._handle(ctx, session, message)
            else:
                result = self._handle(ctx, session, message)
        except ToolLimitExceeded:
            result = ChatResult(
                answer=(
                    "I've reached the limit for one message. Please ask me again "
                    f"in smaller steps, or email {SUPPORT_EMAIL}."
                ),
                route="answer",
            )
        except Exception:
            logger.exception("Unhandled error while answering %r", message)
            result = ChatResult(
                answer=(
                    "Sorry, something went wrong on our side. Please try again, "
                    f"or email {SUPPORT_EMAIL}."
                ),
                route="answer",
            )
        result.tool_calls = list(ctx.calls)
        if result.route == "clarify" and session.clarification is not None:
            # Keep the exact question so an unclear reply can be re-prompted.
            session.clarification["question"] = result.answer
        session.remember("assistant", result.answer)
        return result

    # =============================================================== dispatch
    def _agent_enabled(self, session: Session) -> bool:
        return bool(
            self.settings is not None
            and self.settings.llm_agent
            and self.settings.llm_enabled
            and session.pending is None
            and session.offer is None
        )

    def _handle(self, ctx: ToolContext, session: Session, message: str) -> ChatResult:
        text = message.strip()
        result = ChatResult(answer="")
        intents = nlu.detect_intents(text)

        # 1. A waiting proposal only survives the very next message.
        if session.pending:
            if nlu.is_yes(text) and not self._changes_proposal(text, session.pending):
                pending = session.pending
                session.pending = None
                return self._execute_proposal(ctx, pending, result)
            if nlu.is_yes(text):
                # "Yes, just the power bank" changes what is being returned.
                pending = session.pending
                session.pending = None
                return self._restart(ctx, session, text, pending, result)
            if nlu.is_no(text):
                session.pending = None
                result.answer = (
                    "No problem — I haven't created anything. Is there anything "
                    "else I can help with?"
                )
                return result
            if self._has_new_details(text):
                pending = session.pending
                session.pending = None
                return self._restart(ctx, session, text, pending, result)
            session.pending = None

        # 2. A soft offer (answer to "can I return it?") can be accepted.
        if session.offer:
            offer = session.offer
            session.offer = None
            if nlu.is_yes(text) and not self._changes_proposal(text, offer):
                return self._execute_proposal(ctx, offer, result)
            if nlu.is_yes(text):
                return self._restart(ctx, session, text, offer, result)
            if nlu.is_no(text):
                result.answer = (
                    "No problem — I haven't created anything. Is there anything "
                    "else I can help with?"
                )
                return result

        # 3. Continue the flow we asked a clarifying question about.
        if session.clarification and nlu.looks_off_topic(text):
            session.clarification = None
        if session.clarification and not nlu.looks_off_topic(text):
            clar = session.clarification
            follow = nlu.detect_intents(text)
            if (
                nlu.extract_order_ids(text)
                or nlu.extract_item_ids(text)
                or follow["reason"]
                or nlu.mentions_item_like(text)
            ):
                session.clarification = None
                forced_order = clar.get("order_id")
                forced_reason = follow["reason"] or clar.get("reason")
                forced_item_query = clar.get("item_query")
                if clar.get("kind") == "return":
                    return self._return(
                        ctx, session, text, follow, result,
                        forced_order_id=forced_order,
                        forced_item_ids=clar.get("item_ids") or None,
                        forced_reason=forced_reason,
                        forced_item_query=forced_item_query,
                    )
                if clar.get("kind") == "warranty":
                    return self._warranty(
                        ctx, session, text, follow, result,
                        forced_order_id=forced_order,
                        forced_item_query=forced_item_query,
                    )
                if clar.get("kind") == "cancellation":
                    return self._cancel(
                        ctx, session, text, follow, result,
                        forced_order_id=forced_order,
                    )
            if nlu.is_yes(text) or nlu.is_no(text):
                # "Yes" does not answer "which order/item/reason?"; ask again.
                result.route = "clarify"
                result.answer = (
                    clar.get("question")
                    or "Could you give me the order ID and a little more detail?"
                )
                return result
            session.clarification = None

        if not text:
            return self._out_of_scope(result)

        if (intents["greeting"] and not nlu.has_store_words(text)) or (
            intents["off_topic"] and not nlu.has_store_words(text)
        ):
            return self._out_of_scope(result)

        if not nlu.has_store_words(text) and not intents["order_ids"]:
            return self._out_of_scope(result)

        if re.search(
            r"\b(do you sell|do you stock|in stock|available to buy)\b",
            nlu.normalize(text),
        ):
            result.route = "no_answer"
            result.answer = (
                "The current ShopSphere policies don't cover product availability. "
                f"Please email {SUPPORT_EMAIL} and our team will help."
            )
            return result

        # 3. Pure policy questions come before action flows ("return policy").
        if self._is_policy_only(text, intents):
            return self._policy(ctx, text, result)

        # 4. One message can ask for two things (track + return/policy/...).
        has_second = (
            intents["return"]
            or intents["warranty"]
            or intents["cancel"]
            or intents["policy"]
            or self._looks_like_fault(text)
        )
        if intents["track"] and intents["order_ids"] and has_second:
            tracked = self._track(ctx, text, intents, ChatResult(answer=""))
            if intents["cancel"]:
                action = self._cancel(ctx, session, text, intents, ChatResult(answer=""))
            elif intents["return"]:
                action = self._return(ctx, session, text, intents, ChatResult(answer=""))
            elif intents["warranty"] or self._looks_like_fault(text):
                action = self._warranty(
                    ctx, session, text, intents, ChatResult(answer="")
                )
            else:
                action = self._policy(ctx, text, ChatResult(answer=""))
            result.answer = f"{tracked.answer}\n\n{action.answer}"
            result.route = action.route
            result.citations = action.citations
            result.pending_confirmation = action.pending_confirmation
            return result

        if intents["cancel"]:
            return self._cancel(ctx, session, text, intents, result)

        if intents["return"]:
            return self._return(ctx, session, text, intents, result)

        if intents["warranty"] or self._looks_like_fault(text):
            return self._warranty(ctx, session, text, intents, result)

        if intents["track"]:
            return self._track(ctx, text, intents, result)

        if intents["policy"]:
            return self._policy(ctx, text, result)

        if nlu.has_store_words(text):
            result.route = "clarify"
            result.answer = (
                "I can help with that. Could you tell me the order ID and what "
                "you would like to do — track, return, cancel or check warranty?"
            )
            return result

        return self._out_of_scope(result)

    # ================================================================= return
    def _return(
        self,
        ctx: ToolContext,
        session: Session,
        text: str,
        intents: dict,
        result: ChatResult,
        forced_order_id: str | None = None,
        forced_item_ids: list[str] | None = None,
        forced_reason: str | None = None,
        forced_item_query: str | None = None,
    ) -> ChatResult:
        resolved = self._resolve(
            ctx, text, intents,
            forced_order_id=forced_order_id,
            forced_item_ids=forced_item_ids,
            forced_item_query=forced_item_query,
        )
        if resolved.not_found:
            return self._not_found(result)
        if resolved.no_orders:
            result.route = "no_answer"
            result.answer = "You don't have any orders on your account yet."
            return result
        if resolved.clarify:
            result.route = "clarify"
            result.answer = resolved.clarify
            session.clarification = {
                "kind": "return",
                "order_id": resolved.order["order_id"] if resolved.order else None,
                "item_ids": [],
                "reason": forced_reason or intents["reason"],
                "item_query": forced_item_query or text,
            }
            if not intents["reason"] and not forced_reason:
                result.answer += (
                    " Also, is this a change of mind, or is the item defective?"
                )
            return result

        order = resolved.order
        items = resolved.items
        reason = forced_reason or intents["reason"]

        if not items:
            result.route = "clarify"
            result.answer = self._ask_which_item(order)
            session.clarification = {
                "kind": "return",
                "order_id": order["order_id"],
                "item_ids": [],
                "reason": reason,
                "item_query": forced_item_query or text,
            }
            if not reason:
                result.answer += (
                    " And is this a change of mind, or is the item defective?"
                )
            return result

        if not reason:
            result.route = "clarify"
            result.answer = (
                f"Is the return of {self._names(items)} from order "
                f"{order['order_id']} a change of mind, or is the item defective?"
            )
            session.clarification = {
                "kind": "return",
                "order_id": order["order_id"],
                "item_ids": [item["item_id"] for item in items],
                "reason": None,
                "item_query": forced_item_query or text,
            }
            return result

        item_ids = [item["item_id"] for item in items]
        check = ctx.check_return_eligibility(order["order_id"], item_ids, reason)

        if not check.get("eligible"):
            if check.get("error_code") == "INVALID_REASON":
                session.clarification = {
                    "kind": "return",
                    "order_id": order["order_id"],
                    "item_ids": item_ids,
                    "reason": None,
                    "item_query": forced_item_query or text,
                }
            return self._return_error(ctx, order, items, reason, check, result)

        policy = self._search(ctx, "return window days", limit=3)
        self._cite(result, policy, doc_ids={"returns_policy"}, max_items=1)
        if reason == "defective":
            extra = self._search(ctx, "defective items return window", limit=3)
            self._cite_missing_section(result, extra, {"§5.1"})

        days = check["days_since_delivery"]
        window = check["return_window_days"]
        delivered = order.get("delivery_date")
        if days == 0:
            when = f"It was delivered today, which is within the {window}-day return window."
        elif days == 1:
            when = f"It was delivered yesterday, which is within the {window}-day return window."
        else:
            when = (
                f"It was delivered on {format_date(delivered)} ({days} days ago), "
                f"which is within the {window}-day return window."
            )
        if len(items) > 1:
            when = when.replace("It was", "They were").replace("it was", "they were")

        proposal = Proposal(
            kind="return",
            order_id=order["order_id"],
            item_ids=item_ids,
            item_names=[item.get("name") or item["item_id"] for item in items],
            reason=reason,
            summary=f"Return {', '.join(item_ids)} from {order['order_id']} ({reason})",
            citations=list(result.citations),
            tool_name="create_return_request",
            args={
                "order_id": order["order_id"],
                "item_ids": item_ids,
                "reason": reason,
            },
        )

        names = self._names(items)
        if intents["action"] or forced_order_id:
            session.pending = proposal
            ctx.log_pending("create_return_request", proposal.args)
            result.pending_confirmation = True
            result.answer = (
                f"You can return {names} from order {order['order_id']}. {when} "
                "Shall I go ahead and create the return request?"
            )
        else:
            session.offer = proposal
            result.answer = (
                f"You can return {names} from order {order['order_id']}. {when} "
                "Would you like me to create the return request?"
            )
        return result

    def _return_error(
        self,
        ctx: ToolContext,
        order: dict,
        items: list[dict],
        reason: str,
        check: dict,
        result: ChatResult,
    ) -> ChatResult:
        code = check.get("error_code")
        names = self._names(items)
        days = order.get("days_since_delivery")

        if code == "NON_RETURNABLE":
            policy = self._search(ctx, "gift cards software licences cannot be returned", 3)
            self._cite(result, policy, doc_ids={"returns_policy"}, max_items=1)
            quote = self._quote_from(policy, "§4.1")
            result.answer = f"{names} cannot be returned."
            if quote:
                result.answer += f" {quote}"
        elif code == "OPENED_NOT_RETURNABLE":
            policy = self._search(ctx, "opened in-ear earphones earbuds sealed", 3)
            self._cite(result, policy, doc_ids={"returns_policy"}, max_items=1)
            quote = self._quote_from(policy, "§4.2")
            result.answer = f"{names} has been opened."
            if quote:
                result.answer += f" {quote}"
            result.answer += (
                " If the item is defective instead, I can check that for you."
            )
        elif code == "RETURN_WINDOW_EXPIRED":
            policy = self._search(ctx, "return window days", 3)
            self._cite(result, policy, doc_ids={"returns_policy"}, max_items=1)
            window = check.get("return_window_days")
            result.answer = (
                f"The {window}-day return window for {names} has passed"
                + (f" — it was delivered {days} days ago." if days is not None else ".")
                + " If the item has developed a fault, I can check whether it is "
                "still under warranty."
            )
        elif code == "NOT_DELIVERED":
            result.answer = (
                f"Order {order['order_id']} has not been delivered yet, so it "
                "cannot be returned. You can return it after delivery."
            )
        elif code == "INVALID_ITEM":
            result.answer = (
                f"I couldn't find that item in order {order['order_id']}. "
                "Could you check the item ID?"
            )
        elif code == "INVALID_REASON":
            result.route = "clarify"
            result.answer = (
                "Is this a change of mind, or is the item defective?"
            )
        else:
            result.answer = (
                "I'm sorry, I couldn't check that return. Please contact "
                f"{SUPPORT_EMAIL}."
            )
        return result

    # =============================================================== warranty
    def _warranty(
        self,
        ctx: ToolContext,
        session: Session,
        text: str,
        intents: dict,
        result: ChatResult,
        forced_order_id: str | None = None,
        forced_item_query: str | None = None,
    ) -> ChatResult:
        resolved = self._resolve(
            ctx, text, intents, forced_order_id=forced_order_id,
            forced_item_query=forced_item_query, require_reason=False
        )
        if resolved.not_found:
            return self._not_found(result)
        if resolved.no_orders:
            result.route = "no_answer"
            result.answer = "You don't have any orders on your account yet."
            return result
        if resolved.clarify:
            result.route = "clarify"
            result.answer = resolved.clarify
            session.clarification = {
                "kind": "warranty",
                "order_id": resolved.order["order_id"] if resolved.order else None,
                "item_query": forced_item_query or text,
            }
            return result
        if not resolved.items:
            result.route = "clarify"
            result.answer = self._ask_which_item(resolved.order)
            session.clarification = {
                "kind": "warranty",
                "order_id": resolved.order["order_id"],
                "item_query": forced_item_query or text,
            }
            return result

        order = resolved.order
        items = resolved.items

        if order.get("status") != "delivered" or not order.get("delivery_date"):
            result.answer = (
                f"Order {order['order_id']} has not been delivered yet, so a "
                "warranty claim cannot be started. Please contact us after delivery."
            )
            return result

        covered = [item for item in items if item.get("warranty_active")]
        expired = [
            item for item in items
            if str(item.get("warranty_class") or "").lower() != "none"
            and not item.get("warranty_active")
        ]
        no_warranty = [
            item for item in items
            if str(item.get("warranty_class") or "").lower() == "none"
        ]

        if covered:
            policy = self._search(ctx, "warranty period months", 3)
            self._cite(result, policy, doc_ids={"warranty_policy"}, max_items=2)
            names = self._names(covered)
            months = covered[0].get("warranty_months")
            end = covered[0].get("warranty_end")
            result.answer = (
                f"{names} from order {order['order_id']} is covered by warranty "
                f"until {format_date(end)} ({months}-month warranty)."
            )
            proposal = Proposal(
                kind="warranty",
                order_id=order["order_id"],
                item_ids=[item["item_id"] for item in covered],
                item_names=[item.get("name") or item["item_id"] for item in covered],
                summary=(
                    f"Warranty claim for {', '.join(item['name'] for item in covered)} "
                    f"from order {order['order_id']}: {self._fault_summary(text)}"
                ),
                citations=list(result.citations),
                tool_name="create_support_ticket",
                args={},
            )
            wants_ticket = intents["action"] or "ticket" in nlu.normalize(text) or (
                "claim" in nlu.normalize(text) and "open" in nlu.normalize(text)
            )
            if wants_ticket:
                return self._propose_ticket(
                    ctx, session, proposal, "warranty support ticket", result
                )
            session.offer = proposal
            result.answer += " Would you like me to open a warranty support ticket?"
            return result

        if expired:
            policy = self._search(ctx, "warranty period months", 3)
            self._cite(result, policy, doc_ids={"warranty_policy"}, max_items=1)
            names = self._names(expired)
            if len(expired) == 1:
                ends = f"on {format_date(expired[0].get('warranty_end'))}"
            else:
                ends = "on " + ", ".join(
                    format_date(item.get("warranty_end")) for item in expired
                )
            result.answer = (
                f"The warranty for {names} ended {ends}, so it is no longer "
                "covered. If the item was delivered recently, I can check whether "
                "it is still within the return window instead."
            )
            return result

        policy = self._search(ctx, "products without a warranty", 3)
        self._cite(result, policy, doc_ids={"warranty_policy"}, max_items=1)
        result.answer = (
            f"{self._names(no_warranty)} does not have a warranty, so it cannot "
            "be covered."
        )
        return result

    # =========================================================== cancellation
    def _cancel(
        self,
        ctx: ToolContext,
        session: Session,
        text: str,
        intents: dict,
        result: ChatResult,
        forced_order_id: str | None = None,
    ) -> ChatResult:
        order_ids = intents["order_ids"]
        order_id = order_ids[0] if order_ids else forced_order_id

        if not order_id:
            resp = ctx.get_orders(status="processing")
            orders = resp.get("orders", [])
            session.clarification = {"kind": "cancellation", "order_id": None}
            if orders:
                listing = "; ".join(
                    f"{order['order_id']} ({self._names(order['items'])})"
                    for order in orders
                )
                result.route = "clarify"
                result.answer = (
                    "Which order would you like to cancel? These orders are still "
                    f"processing: {listing}."
                )
            else:
                result.route = "clarify"
                result.answer = (
                    "Which order would you like to cancel? Please give me the order "
                    "ID. Orders can only be cancelled while they are processing."
                )
            return result

        resp = ctx.get_orders(order_id=order_id)
        if resp.get("error_code"):
            return self._not_found(result)
        order = resp["orders"][0]

        if order.get("status") == "cancelled":
            result.answer = f"Order {order_id} has already been cancelled."
            return result

        if order.get("status") != "processing":
            policy = self._search(ctx, "cannot cancel an order after it has shipped", 3)
            self._cite(
                result,
                policy,
                doc_ids={"order_cancellation"},
                max_items=1,
                sections={"§1.2"},
            )
            if order.get("status") == "delivered":
                result.answer = (
                    f"Order {order_id} has already been delivered, so it cannot "
                    "be cancelled. You can return it under the Returns Policy."
                )
            else:
                result.answer = (
                    f"Order {order_id} has already shipped, so it cannot be "
                    "cancelled. You can return it after delivery under the "
                    "Returns Policy."
                )
            return result

        policy = self._search(ctx, "cancel order before shipment", 3)
        self._cite(result, policy, doc_ids={"order_cancellation"}, max_items=1)

        result.answer = (
            f"Order {order_id} is still processing, so it can be cancelled."
        )

        proposal = Proposal(
            kind="cancellation",
            order_id=order_id,
            summary=f"Customer requested cancellation of order {order_id}.",
            citations=list(result.citations),
            tool_name="create_support_ticket",
            args={},
        )
        if intents["action"]:
            return self._propose_ticket(
                ctx, session, proposal, "cancellation ticket", result
            )
        session.offer = proposal
        result.answer = (
            f"Order {order_id} is still processing, so it can be cancelled. "
            "Would you like me to open a cancellation ticket?"
        )
        return result

    # ============================================================== proposal
    def _propose_ticket(
        self,
        ctx: ToolContext,
        session: Session,
        proposal: Proposal,
        label: str,
        result: ChatResult,
    ) -> ChatResult:
        args = {
            "order_id": proposal.order_id,
            "ticket_type": proposal.kind,
            "summary": proposal.summary,
        }
        proposal.tool_name = "create_support_ticket"
        proposal.args = args
        session.pending = proposal
        ctx.log_pending("create_support_ticket", args)
        result.pending_confirmation = True
        intro = result.answer.strip()
        if intro:
            intro += " "
        result.answer = f"{intro}Shall I go ahead and open the {label}?"
        return result

    def _execute_proposal(
        self, ctx: ToolContext, proposal: Proposal, result: ChatResult
    ) -> ChatResult:
        if proposal.kind == "return":
            outcome = ctx.create_return_request(
                proposal.order_id, proposal.item_ids, proposal.reason or ""
            )
        else:
            outcome = ctx.create_support_ticket(
                proposal.order_id,
                proposal.kind,
                proposal.summary,
            )

        result.citations = list(proposal.citations)

        if outcome.get("error_code"):
            result.answer = self._error_explanation(outcome)
            return result

        if proposal.kind == "return":
            ref = outcome["request_id"]
            result.answer = (
                f"Done — the return request has been created for order "
                f"{proposal.order_id}. Your reference is {ref}. We'll be in touch "
                "with the pickup details."
            )
        elif proposal.kind == "warranty":
            ref = outcome["ticket_id"]
            result.answer = (
                f"Done — your warranty support ticket has been created for order "
                f"{proposal.order_id}. Your reference is {ref}. Our service team "
                "will contact you to arrange inspection."
            )
        else:
            ref = outcome["ticket_id"]
            result.answer = (
                f"Done — a cancellation ticket has been created for order "
                f"{proposal.order_id}. Your reference is {ref}. Our team will "
                "confirm the cancellation by email."
            )
        return result

    def _error_explanation(self, outcome: dict) -> str:
        code = outcome.get("error_code")
        return {
            "ORDER_NOT_FOUND": NOT_FOUND_ANSWER,
            "RETURN_WINDOW_EXPIRED": (
                "I couldn't create the return because the return window has now "
                "expired. Nothing was saved."
            ),
            "OPENED_NOT_RETURNABLE": (
                "I couldn't create the return because the item was opened and can "
                "only be returned while sealed for a change of mind. Nothing was saved."
            ),
            "NON_RETURNABLE": (
                "I couldn't create the return because this product cannot be "
                "returned. Nothing was saved."
            ),
            "NOT_DELIVERED": (
                "I couldn't create this because the order has not been delivered yet. "
                "Nothing was saved."
            ),
            "ORDER_ALREADY_SHIPPED": (
                "I couldn't create the ticket because the order has already shipped. "
                "Nothing was saved."
            ),
            "WARRANTY_EXPIRED": (
                "I couldn't create the ticket because the warranty has expired. "
                "Nothing was saved."
            ),
            "NO_WARRANTY": (
                "I couldn't create the ticket because these products have no warranty. "
                "Nothing was saved."
            ),
        }.get(code, "I couldn't complete that action. Nothing was saved.")

    # ================================================================= track
    def _track(
        self, ctx: ToolContext, text: str, intents: dict, result: ChatResult
    ) -> ChatResult:
        order_ids = intents["order_ids"]
        if order_ids:
            resp = ctx.get_orders(order_id=order_ids[0])
            if resp.get("error_code"):
                return self._not_found(result)
            orders = resp["orders"]
        else:
            resp = ctx.get_orders()
            orders = resp.get("orders", [])

        if not orders:
            result.route = "no_answer"
            result.answer = "You don't have any orders on your account yet."
            return result

        if len(orders) > 1:
            listing = "; ".join(
                f"{order['order_id']} — {self._status_line(order).rstrip('.')}"
                for order in orders[:8]
            )
            result.route = "clarify"
            result.answer = (
                "I found more than one order on your account. Which one do you mean? "
                f"{listing}."
            )
            return result

        result.answer = self._status_line(orders[0])
        return result

    def _status_line(self, order: dict) -> str:
        order_id = order["order_id"]
        status = order.get("status")
        shipment = order.get("shipment") or {}
        ship_status = shipment.get("status") or status

        if status == "delivered":
            line = f"Order {order_id} was delivered on {format_date(order.get('delivery_date'))}."
            days = order.get("days_since_delivery")
            if days is not None:
                line += f" That was {days} day{'s' if days != 1 else ''} ago."
            return line
        if status == "cancelled":
            return f"Order {order_id} was cancelled."
        if status == "processing":
            return (
                f"Order {order_id} is still processing and has not shipped yet."
            )

        line = (
            f"Order {order_id} is {STATUS_WORDS.get(ship_status, ship_status.replace('_', ' '))}"
        )
        if shipment.get("carrier"):
            line += f" with {shipment['carrier']}"
        line += "."
        eta = parse_date(shipment.get("eta"))
        if eta:
            line += f" The estimated delivery date is {format_date(eta)}."
        if ship_status == "delayed" and shipment.get("original_eta"):
            line += (
                f" The original estimate was {format_date(shipment.get('original_eta'))}."
            )
        if shipment.get("tracking_no"):
            line += f" Tracking number: {shipment['tracking_no']}."
        return line

    # ================================================================ policy
    def _policy(self, ctx: ToolContext, text: str, result: ChatResult) -> ChatResult:
        query = self._expand_policy_query(text)
        matches = self._search(ctx, query, limit=3)
        if not matches:
            future = self._search_future(ctx, query, limit=1)
            if future:
                item = future[0]
                result.answer = (
                    f"The current policies do not cover that yet. "
                    f"{item['title']} takes effect on "
                    f"{format_date(item['effective_date'])}."
                )
                return result
            result.route = "no_answer"
            result.answer = (
                "The current ShopSphere policies don't cover that, so I don't want "
                f"to guess. Please email {SUPPORT_EMAIL} and our team will help."
            )
            return result

        parts = []
        selected = self._select_policy_chunks(matches)
        for item in selected:
            label = f"{item['title']} {item['section']}".strip()
            parts.append(f"{label} says: “{trim_sentences(item['text'])}”")
            citation = {"doc_id": item["doc_id"], "section": item["section"]}
            if citation not in result.citations:
                result.citations.append(citation)
        result.answer = "\n\n".join(parts)

        future = self._search_future(ctx, query, limit=1)
        asks_money = re.search(
            r"\b(fee|fees|cost|costs|charge|charges|price|how much|threshold)\b",
            nlu.normalize(text),
        )
        if (
            future
            and asks_money
            and future[0]["score"] >= 0.5 * matches[0]["score"]
        ):
            item = future[0]
            result.answer += (
                f"\n\nNote: {item['title']} takes effect on "
                f"{format_date(item['effective_date'])}. It says: "
                f"“{trim_sentences(item['text'], 220)}”"
            )
        return result

    # ============================================================= utilities
    def _resolve(
        self,
        ctx: ToolContext,
        text: str,
        intents: dict,
        forced_order_id: str | None = None,
        forced_item_ids: list[str] | None = None,
        forced_item_query: str | None = None,
        require_reason: bool = True,
    ) -> Resolution:
        order_ids = intents["order_ids"]
        item_ids = intents["item_ids"]
        fallback_item_ids = forced_item_ids or []
        order_id = order_ids[0] if order_ids else forced_order_id

        if item_ids and not order_id:
            order_id = item_ids[0].rsplit("-", 1)[0]

        if order_id:
            resp = ctx.get_orders(order_id=order_id)
            if resp.get("error_code"):
                return Resolution(not_found=True)
            order = resp["orders"][0]
            if item_ids:
                known = {item["item_id"] for item in order["items"]}
                bad = [item_id for item_id in item_ids if item_id not in known]
                if bad:
                    return Resolution(
                        order=order,
                        bad_items=bad,
                        clarify=(
                            f"Item {bad[0]} is not in order {order_id}. "
                            f"Could you check the item ID?"
                        ),
                    )
                items = [
                    item for item in order["items"] if item["item_id"] in item_ids
                ]
                return Resolution(order=order, items=items)

            items = nlu.match_items(order, text)
            if not items and forced_item_query:
                items = nlu.match_items(order, forced_item_query)
            if not items and fallback_item_ids:
                items = [
                    item for item in order["items"]
                    if item["item_id"] in fallback_item_ids
                ]
            if not items and len(order["items"]) == 1:
                items = order["items"]
            if not items and len(order["items"]) > 1:
                return Resolution(
                    order=order, clarify=self._ask_which_item(order)
                )
            return Resolution(order=order, items=items)

        # No order ID: search the customer's orders for the mentioned product.
        resp = ctx.get_orders()
        orders = resp.get("orders", [])
        if not orders:
            return Resolution(no_orders=True)

        matches: list[tuple[dict, list[dict]]] = []
        for order in orders:
            items = nlu.match_items(order, text)
            if items:
                matches.append((order, items))

        if len(matches) == 1:
            return Resolution(order=matches[0][0], items=matches[0][1])
        if len(matches) > 1:
            listing = "; ".join(
                f"{order['order_id']} ({self._names(items)})"
                for order, items in matches[:6]
            )
            return Resolution(
                clarify=(
                    "I found that item in more than one order: "
                    f"{listing}. Which order do you mean?"
                )
            )

        if len(orders) == 1:
            order = orders[0]
            items = order["items"] if len(order["items"]) == 1 else []
            if not items:
                return Resolution(order=order, clarify=self._ask_which_item(order))
            return Resolution(order=order, items=items)

        listing = "; ".join(
            f"{order['order_id']} ({order.get('status')})" for order in orders[:8]
        )
        return Resolution(
            clarify=(
                "Which order is this for? I can see: "
                f"{listing}."
            )
        )

    def _ask_which_item(self, order: dict) -> str:
        listing = "; ".join(
            f"{item['item_id']} — {item['name']}" for item in order["items"]
        )
        return (
            f"Which item from order {order['order_id']} do you mean? "
            f"It contains: {listing}."
        )

    def _restart(
        self,
        ctx: ToolContext,
        session: Session,
        text: str,
        pending: Proposal,
        result: ChatResult,
    ) -> ChatResult:
        intents = nlu.detect_intents(text)
        intents["action"] = True
        if pending.kind == "return":
            return self._return(
                ctx,
                session,
                text,
                intents,
                result,
                forced_order_id=pending.order_id,
                forced_reason=nlu.detect_reason(text) or pending.reason,
            )
        if pending.kind == "warranty":
            return self._warranty(
                ctx, session, text, intents, result, forced_order_id=pending.order_id
            )
        return self._cancel(
            ctx, session, text, intents, result, forced_order_id=pending.order_id
        )

    def _has_new_details(self, text: str) -> bool:
        if nlu.extract_order_ids(text) or nlu.extract_item_ids(text):
            return True
        return nlu.mentions_item_like(text) or nlu.detect_reason(text) is not None

    def _changes_proposal(self, text: str, pending: Proposal) -> bool:
        """Whether a yes-message also asks to change the waiting proposal.

        A plain "yes" keeps the proposal. Explicitly different items, a
        different order or a different reason must start a new proposal instead
        of silently acting on the old one.
        """
        order_ids = nlu.extract_order_ids(text)
        if order_ids and pending.order_id not in order_ids:
            return True

        item_ids = nlu.extract_item_ids(text)
        if item_ids and set(item_ids) != set(pending.item_ids):
            return True

        reason = nlu.detect_reason(text)
        if reason and pending.reason and reason != pending.reason:
            return True

        normalized = nlu.normalize(text)
        if nlu.mentions_item_like(text) and any(
            word in normalized for word in ("just", "only", "instead", "not the")
        ):
            if len(pending.item_ids) > 1:
                return True
            if pending.item_names:
                name_tokens = set(
                    re.findall(r"[a-z0-9]+", " ".join(pending.item_names).lower())
                )
                text_tokens = set(re.findall(r"[a-z0-9]+", normalized))
                if name_tokens & text_tokens:
                    return False
            return True
        return False

    def _is_policy_only(self, text: str, intents: dict) -> bool:
        normalized = nlu.normalize(text)
        has_specific = bool(intents["order_ids"] or intents["item_ids"])

        # A general question about a rule is a policy question, even when it
        # uses an action word ("Can I cancel after it has shipped?"). A
        # possessive plus a concrete product ("my earbuds") points at the
        # customer's own order, so that stays in the order-specific flow.
        possessive = bool(re.search(r"\b(my|our)\b", normalized))
        own_product = possessive and nlu.mentions_product(text)
        if not has_specific and not intents["action"] and nlu.is_question(text):
            if intents["cancel"]:
                return True
            if intents["return"] and not own_product:
                return True
            if intents["warranty"] and not own_product:
                return True
            if intents["track"] and re.search(
                r"\b(how do i|how can i|how to|where can i|what is the tracking)\b",
                normalized,
            ):
                return True

        if ("policy" in normalized or "policies" in normalized) and not intents["action"]:
            return True
        if intents["track"] or intents["order_ids"]:
            return False
        if (
            "refund" in normalized
            and not nlu.mentions_item_like(text)
            and re.match(
                r"^(do|does|can|is|are|what|how|when|where|will|would|should)\b",
                normalized,
            )
        ):
            return True
        store = nlu.has_store_words(text)
        if store and any(
            phrase in normalized
            for phrase in (
                "how many days", "how long", "how much", "warranty period",
                "return window", "what is the", "what's the",
            )
        ):
            return True
        if store and not any(
            intents[key] for key in ("return", "warranty", "cancel", "track")
        ):
            if re.search(
                r"\b(shipping|delivery|address|payment|refund|exchange|lost|"
                r"delay|late|fee|cost|charge|tracking)\b",
                normalized,
            ):
                return True
        return False

    def _select_policy_chunks(self, matches: list[dict]) -> list[dict]:
        """Keep the answer focused: top paragraph, then a closely related one
        from the same document when possible, else another strong match."""
        if not matches:
            return []
        top = matches[0]
        chosen = [top]
        for item in matches[1:]:
            if (
                item["doc_id"] == top["doc_id"]
                and item["score"] >= 0.5 * top["score"]
            ):
                chosen.append(item)
                break
        if len(chosen) == 1:
            for item in matches[1:]:
                if (
                    item["doc_id"] != top["doc_id"]
                    and item["score"] >= 0.95 * top["score"]
                ):
                    chosen.append(item)
                    break
        return chosen

    def _expand_policy_query(self, text: str) -> str:
        """Add topic words so broad questions rank the substantive section.

        This only rewrites the search query; no policy facts live here.
        """
        normalized = nlu.normalize(text)
        additions: list[str] = []
        if "return" in normalized and any(
            word in normalized for word in ("policy", "how many", "how long", "window")
        ):
            additions.append("return window days")
        if "refund" in normalized and any(
            word in normalized
            for word in ("policy", "how long", "take", "offer", "accept", "get")
        ):
            additions.append("refund processing business days")
        if "warranty" in normalized and "policy" in normalized:
            additions.append("warranty period months")
        if "shipping" in normalized and any(
            word in normalized for word in ("policy", "fee", "cost", "how much")
        ):
            additions.append("shipping fee")
        if "cancel" in normalized:
            additions.append("cancel before shipment")
        return (text + " " + " ".join(additions)).strip()

    def _looks_like_fault(self, text: str) -> bool:
        normalized = nlu.normalize(text)
        return any(phrase in normalized for phrase in nlu.DEFECTIVE_PHRASES)

    def _fault_summary(self, text: str) -> str:
        cleaned = text.strip()
        return cleaned[:200] if cleaned else "fault reported"

    def _names(self, items: list[dict]) -> str:
        names = [item.get("name") or item.get("item_id", "item") for item in items]
        if not names:
            return "the item"
        if len(names) == 1:
            return names[0]
        if len(names) == 2:
            return f"{names[0]} and {names[1]}"
        return ", ".join(names[:-1]) + f" and {names[-1]}"

    def _search(self, ctx: ToolContext, query: str, limit: int = 3) -> list[dict]:
        return ctx.search_policy(query, limit=limit)["results"]

    def _search_future(self, ctx: ToolContext, query: str, limit: int = 1) -> list[dict]:
        matches = ctx.index.search_future(query, limit=limit)
        return [
            {
                "doc_id": item.chunk.doc_id,
                "title": item.chunk.title,
                "section": item.chunk.section,
                "text": item.chunk.text,
                "effective_date": item.chunk.effective_date.isoformat(),
                "score": item.score,
            }
            for item in matches
        ]

    def _quote_from(self, matches: list[dict], section: str) -> str:
        for item in matches:
            if item["section"] == section:
                return trim_sentences(item["text"], 300)
        return ""

    def _cite(
        self,
        result: ChatResult,
        matches: list[dict],
        doc_ids: set[str] | None = None,
        max_items: int = 2,
        sections: set[str] | None = None,
    ) -> None:
        added = 0
        for item in matches:
            if doc_ids and item["doc_id"] not in doc_ids:
                continue
            if sections and item["section"] not in sections:
                continue
            citation = {"doc_id": item["doc_id"], "section": item["section"]}
            if citation not in result.citations:
                result.citations.append(citation)
                added += 1
            if added >= max_items:
                return

    def _cite_missing_section(
        self, result: ChatResult, matches: list[dict], sections: set[str]
    ) -> None:
        for item in matches:
            if item["section"] in sections:
                citation = {"doc_id": item["doc_id"], "section": item["section"]}
                if citation not in result.citations:
                    result.citations.append(citation)
                return

    def _not_found(self, result: ChatResult) -> ChatResult:
        result.answer = NOT_FOUND_ANSWER
        result.route = "no_answer"
        result.citations = []
        result.pending_confirmation = False
        return result

    def _out_of_scope(self, result: ChatResult) -> ChatResult:
        result.route = "out_of_scope"
        result.citations = []
        result.tool_calls = []
        result.pending_confirmation = False
        result.answer = (
            "I can only help with ShopSphere orders and policies. I can track an "
            "order, start a return, check warranty cover, cancel an order, or "
            "explain a store policy."
        )
        return result
