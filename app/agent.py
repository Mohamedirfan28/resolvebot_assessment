"""Optional LLM tool-calling loop.

This is off by default. Set ``LLM_AGENT=1`` together with a provider and API key
to let the model choose tools. Python still owns every business decision:

* the model may only call the five real tools;
* write tools are intercepted. ``create_return_request`` and
  ``create_support_ticket`` are validated here and turned into a waiting
  proposal; they are never executed in the same turn;
* the confirmation ("yes") is handled by the deterministic assistant, which
  re-runs the rule check before inserting;
* a provider error falls back to the deterministic planner.

If no key is configured, or ``LLM_AGENT`` is unset, ``app.assistant`` never
calls this module.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field

import httpx

from . import nlu, rules
from .assistant_types import ChatResult
from .config import Settings
from .sessions import Proposal, Session
from .tools import ToolContext, ToolLimitExceeded
from .util import format_date

logger = logging.getLogger("resolvebot.agent")

NOT_FOUND_ANSWER = "I couldn't find that order on your account."
SUPPORT_EMAIL = "support@shopsphere.example.com"
ROUTES = {"answer", "clarify", "no_answer", "out_of_scope"}
WRITE_TOOLS = {"create_return_request", "create_support_ticket"}

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "google": "gemini-1.5-flash",
}


@dataclass
class LLMReply:
    """One provider turn: some prose and/or tool calls."""

    text: str = ""
    tool_calls: list[dict] = field(default_factory=list)


# --------------------------------------------------------------- tool schemas
TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "get_orders",
            "description": (
                "Read the customer's own orders, with items, delivery date, days "
                "since delivery and warranty dates. Both arguments are optional."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "Order ID like SS-10458"},
                    "status": {
                        "type": "string",
                        "enum": ["processing", "shipped", "delivered", "cancelled"],
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": (
                "Search the current, effective ShopSphere policy documents. "
                "Returns matching paragraphs with doc_id and section for citation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to look up"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_return_eligibility",
            "description": (
                "Read-only check: can these items from this order be returned for "
                "this reason? Returns eligible or an error code. Call before "
                "proposing a return."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {
                        "type": "string",
                        "enum": ["changed_mind", "defective"],
                    },
                },
                "required": ["order_id", "item_ids", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_return_request",
            "description": (
                "Create a return request. Only call this when the customer clearly "
                "asks for the action. The system will ask for confirmation; do not "
                "tell the customer it is done."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "item_ids": {"type": "array", "items": {"type": "string"}},
                    "reason": {
                        "type": "string",
                        "enum": ["changed_mind", "defective"],
                    },
                },
                "required": ["order_id", "item_ids", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_support_ticket",
            "description": (
                "Open a warranty or cancellation ticket. Only call this when the "
                "customer clearly asks for the action. The system will ask for "
                "confirmation; do not tell the customer it is done."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string"},
                    "ticket_type": {
                        "type": "string",
                        "enum": ["warranty", "cancellation"],
                    },
                    "summary": {"type": "string"},
                },
                "required": ["order_id", "ticket_type", "summary"],
            },
        },
    },
]


def system_prompt(settings: Settings) -> str:
    today = format_date(settings.app_today)
    return f"""You are ResolveBot, the customer-support assistant for ShopSphere, \
an online electronics store. Today is {today}.

Rules you must follow:
- Use the tools to read facts. Never invent an order, a date, a price, a \
delivery status or a policy rule.
- The customer's identity is fixed by the request header. Never ask for it and \
never look up an order for anyone else.
- When the customer asks about a policy, call search_policy first. Quote only \
the paragraphs it returns and cite them as doc_id + section. If the current \
policies do not answer the question, use route no_answer and suggest emailing \
{SUPPORT_EMAIL}.
- If a specific order the customer mentions is missing or belongs to someone \
else, reply exactly: "{NOT_FOUND_ANSWER}" with route no_answer and no citations.
- If the order, item or return reason is missing or unclear, ask for all missing \
details in one message and use route clarify. If several orders or items match, \
list them and ask which one. Never guess.
- A return reason must be changed_mind or defective. If the customer does not \
say which, ask.
- For a return, call check_return_eligibility before proposing anything. If it \
returns an error, explain the reason in plain words and propose nothing.
- Writes need two steps. Only call create_return_request or \
create_support_ticket when the customer clearly asks for the action ("please \
return it"). If they only ask a question ("can I return it?"), answer it and \
offer to do the action without calling a write tool. When you do call a write \
tool the system asks the customer to confirm; never claim the action is already \
done.
- A ticket is only for warranty or cancellation. Cancellation is only possible \
while the order is processing.
- Off-topic messages (weather, jokes, general chat) get route out_of_scope and \
one short line about what you can help with.
- Write dates like "3 July 2026".

Return your final answer as one JSON object and nothing else:
{{"answer": "...", "route": "answer|clarify|no_answer|out_of_scope", \
"citations": [{{"doc_id": "...", "section": "§x.y"}}]}}
Citations must come only from paragraphs returned by search_policy. Use an empty \
list when no policy text is used."""


# ------------------------------------------------------------------- entry
def run(
    settings: Settings,
    ctx: ToolContext,
    session: Session,
    message: str,
    complete_fn=None,
    timeout_s: float = 24.0,
) -> ChatResult:
    """Run the LLM tool loop for one message.

    ``complete_fn`` is injectable so tests never touch the network.
    """
    complete = complete_fn or complete_chat

    # Assistant.handle() already remembered the current user message.
    history = list(session.messages[:-1])
    neutral: list[dict] = [{"role": "system", "content": system_prompt(settings)}]
    neutral.extend(history)
    neutral.append({"role": "user", "content": message})

    requested_ids = set(nlu.extract_order_ids(message))
    missing_ids: set[str] = set()
    citations: list[dict] = []

    deadline = time.monotonic() + timeout_s
    max_steps = max(2, ctx.max_tool_calls + 2)

    for _ in range(max_steps):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            break
        try:
            reply = complete(
                settings, neutral, TOOL_SPECS, timeout_s=min(20.0, remaining)
            )
        except Exception:
            logger.exception("LLM provider call failed")
            raise

        calls = reply.tool_calls or []
        if not calls:
            return _final(reply.text, citations, missing_ids)

        neutral.append(
            {
                "role": "assistant",
                "content": reply.text or None,
                "tool_calls": calls,
            }
        )

        for call in calls:
            name, args, call_id = _call_parts(call)

            if name in WRITE_TOOLS:
                outcome = _handle_write(
                    ctx, session, message, name, args, citations
                )
                if isinstance(outcome, ChatResult):
                    return outcome
                neutral.append(_tool_message(call_id, name, outcome))
                continue

            result = _execute_read(ctx, name, args)
            if result.get("error_code") == "ORDER_NOT_FOUND":
                order_id = args.get("order_id")
                if order_id in requested_ids:
                    missing_ids.add(order_id)
            _collect_citations(result, citations)
            neutral.append(_tool_message(call_id, name, result))

    if missing_ids:
        return ChatResult(answer=NOT_FOUND_ANSWER, route="no_answer")
    return ChatResult(
        answer=(
            "I've reached the limit for one message. Please ask me again in "
            f"smaller steps, or email {SUPPORT_EMAIL}."
        ),
        route="answer",
        citations=citations,
    )


# ----------------------------------------------------------- tool execution
def _execute_read(ctx: ToolContext, name: str, args: dict) -> dict:
    try:
        if name == "get_orders":
            order_id = args.get("order_id")
            status = args.get("status")
            return ctx.get_orders(
                order_id=str(order_id) if order_id else None,
                status=str(status) if status else None,
            )
        if name == "search_policy":
            return ctx.search_policy(str(args.get("query") or ""))
        if name == "check_return_eligibility":
            order_id, item_ids, reason = _return_args(args)
            return ctx.check_return_eligibility(order_id, item_ids, reason)
    except ToolLimitExceeded:
        raise
    except Exception:
        logger.exception("Tool %s failed", name)
        return {"error_code": "INVALID_ITEM", "message": "The tool call was invalid"}
    return {"error_code": "INVALID_ITEM", "message": f"Unknown tool {name}"}


def _handle_write(
    ctx: ToolContext,
    session: Session,
    message: str,
    name: str,
    args: dict,
    citations: list[dict],
) -> ChatResult | dict:
    """Validate a write and turn it into a proposal. Never writes."""
    if _is_pure_question(message):
        return {
            "pending_confirmation": False,
            "message": (
                "The customer asked a question, not for the action. Answer the "
                "question and offer the action; do not create anything."
            ),
        }

    if name == "create_return_request":
        order_id, item_ids, reason = _return_args(args)
        if not order_id or not item_ids:
            return {
                "error_code": "INVALID_ITEM",
                "message": "order_id and a non-empty item_ids list are required",
            }
        if reason not in rules.VALID_REASONS:
            return {
                "error_code": "INVALID_REASON",
                "message": "reason must be changed_mind or defective",
            }
        order = ctx.lookup_order(order_id)
        if order is None:
            return ChatResult(answer=NOT_FOUND_ANSWER, route="no_answer")
        check = rules.evaluate_return(order, item_ids, reason, ctx.params)
        if not check.get("eligible"):
            return ChatResult(
                answer=_explain_error(check),
                route="answer",
                citations=list(citations),
            )
        proposal = Proposal(
            kind="return",
            order_id=order_id,
            item_ids=item_ids,
            item_names=[
                item.get("name") or item["item_id"]
                for item in order.get("items", [])
                if item["item_id"] in item_ids
            ],
            reason=reason,
            citations=list(citations),
            tool_name=name,
            args={"order_id": order_id, "item_ids": item_ids, "reason": reason},
        )
        answer = _return_proposal_answer(order, item_ids, reason)
        return _propose(ctx, session, proposal, answer)

    # create_support_ticket
    order_id = str(args.get("order_id") or "")
    ticket_type = str(args.get("ticket_type") or "")
    summary = str(args.get("summary") or "").strip()
    if ticket_type not in {"warranty", "cancellation"}:
        return {
            "error_code": "INVALID_TICKET_TYPE",
            "message": "ticket_type must be warranty or cancellation",
        }
    if not order_id:
        return {
            "error_code": "ORDER_NOT_FOUND",
            "message": "order_id is required",
        }
    order = ctx.lookup_order(order_id)
    if order is None:
        return ChatResult(answer=NOT_FOUND_ANSWER, route="no_answer")

    if ticket_type == "cancellation":
        check = rules.evaluate_cancellation(order)
        default_summary = f"Cancellation requested for order {order_id}"
    else:
        check = rules.evaluate_warranty(order, None, ctx.params)
        covered = ", ".join(
            entry.get("name") or entry["item_id"] for entry in check.get("covered", [])
        )
        default_summary = f"Warranty claim for {covered} from order {order_id}"

    if not check.get("eligible"):
        return ChatResult(
            answer=_explain_error(check),
            route="answer",
            citations=list(citations),
        )

    summary = summary or default_summary
    proposal = Proposal(
        kind=ticket_type,
        order_id=order_id,
        summary=summary,
        citations=list(citations),
        tool_name=name,
        args={
            "order_id": order_id,
            "ticket_type": ticket_type,
            "summary": summary,
        },
    )
    if ticket_type == "warranty":
        answer = (
            f"I can open a warranty support ticket for order {order_id}. "
            "Shall I go ahead?"
        )
    else:
        answer = (
            f"I can open a cancellation ticket for order {order_id}. "
            "Shall I go ahead?"
        )
    return _propose(ctx, session, proposal, answer)


def _propose(
    ctx: ToolContext, session: Session, proposal: Proposal, answer: str
) -> ChatResult:
    ctx.log_pending(proposal.tool_name, dict(proposal.args))
    session.pending = proposal
    session.clarification = None
    return ChatResult(
        answer=answer,
        route="answer",
        citations=list(proposal.citations),
        pending_confirmation=True,
    )


# --------------------------------------------------------------- formatting
def _final(
    text: str, seen: list[dict], missing_ids: set[str]
) -> ChatResult:
    if missing_ids:
        return ChatResult(answer=NOT_FOUND_ANSWER, route="no_answer")

    text = (text or "").strip()
    payload = _extract_json(text)

    if payload is None:
        return ChatResult(answer=text or _fallback(), route="answer")

    answer = str(payload.get("answer") or "").strip() or _fallback()
    route = str(payload.get("route") or "answer").strip().lower()
    if route not in ROUTES:
        route = "answer"
    if route in {"no_answer", "out_of_scope"}:
        cites: list[dict] = []
    else:
        cites = _valid_citations(payload.get("citations"), seen)

    return ChatResult(answer=answer, route=route, citations=cites)


def _extract_json(text: str) -> dict | None:
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    try:
        payload = json.loads(candidate)
    except Exception:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            payload = json.loads(candidate[start : end + 1])
        except Exception:
            return None
    return payload if isinstance(payload, dict) else None


def _valid_citations(raw, seen: list[dict]) -> list[dict]:
    allowed = {(c["doc_id"], c["section"]) for c in seen}
    result: list[dict] = []
    if not isinstance(raw, list):
        return result
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        pair = (entry.get("doc_id"), entry.get("section"))
        if pair in allowed and pair not in {(c["doc_id"], c["section"]) for c in result}:
            result.append({"doc_id": pair[0], "section": pair[1]})
    return result


def _collect_citations(result: dict, citations: list[dict]) -> None:
    for item in result.get("results") or []:
        if not isinstance(item, dict):
            continue
        doc_id, section = item.get("doc_id"), item.get("section")
        if not doc_id or not section:
            continue
        entry = {"doc_id": doc_id, "section": section}
        if entry not in citations:
            citations.append(entry)


def _return_proposal_answer(order: dict, item_ids: list[str], reason: str) -> str:
    names = {item["item_id"]: item.get("name") or item["item_id"] for item in order.get("items", [])}
    label = ", ".join(names.get(item_id, item_id) for item_id in item_ids)
    why = "change of mind" if reason == "changed_mind" else "the item is defective"
    return (
        f"I can create a return request for {label} from order "
        f"{order['order_id']} ({why}). Shall I go ahead?"
    )


def _explain_error(outcome: dict) -> str:
    code = outcome.get("error_code")
    return {
        "ORDER_NOT_FOUND": NOT_FOUND_ANSWER,
        "RETURN_WINDOW_EXPIRED": (
            "I can't create the return because the return window has expired. "
            "Nothing has been saved."
        ),
        "OPENED_NOT_RETURNABLE": (
            "I can't create the return because the item was opened and can only "
            "be returned while sealed for a change of mind. A defective return is "
            "still possible. Nothing has been saved."
        ),
        "NON_RETURNABLE": (
            "I can't create the return because this product cannot be returned. "
            "Nothing has been saved."
        ),
        "NOT_DELIVERED": (
            "I can't create this because the order has not been delivered yet. "
            "Nothing has been saved."
        ),
        "ORDER_ALREADY_SHIPPED": (
            "I can't create that ticket because the order has already shipped. "
            "Nothing has been saved."
        ),
        "WARRANTY_EXPIRED": (
            "I can't create that ticket because the warranty has expired. "
            "Nothing has been saved."
        ),
        "NO_WARRANTY": (
            "I can't create that ticket because these products have no warranty. "
            "Nothing has been saved."
        ),
        "INVALID_ITEM": (
            "I can't do that because one of the items is not in this order. "
            "Nothing has been saved."
        ),
        "INVALID_REASON": (
            "I need to know whether this is a change of mind or the item is "
            "defective. Nothing has been saved."
        ),
        "INVALID_TICKET_TYPE": (
            "I can only open a warranty or cancellation ticket. Nothing has been saved."
        ),
    }.get(code, "I can't complete that action. Nothing has been saved.")


def _fallback() -> str:
    return (
        "Sorry, I couldn't put that answer together. Please try again, or email "
        f"{SUPPORT_EMAIL}."
    )


def _is_pure_question(message: str) -> bool:
    text = nlu.normalize(message)
    return bool(nlu.QUESTION_RE.search(text)) and not bool(nlu.ACTION_RE.search(text))


def _call_parts(call: dict) -> tuple[str, dict, str]:
    call_id = str(call.get("id") or f"call_{uuid.uuid4().hex[:8]}")
    name = str(call.get("name") or "").strip()
    args = call.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args or "{}")
        except Exception:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return name, args, call_id


def _return_args(args: dict) -> tuple[str, list[str], str]:
    order_id = str(args.get("order_id") or "").strip()
    raw_items = args.get("item_ids")
    if isinstance(raw_items, str):
        raw_items = [raw_items]
    item_ids = [str(item) for item in raw_items] if isinstance(raw_items, list) else []
    reason = str(args.get("reason") or "").strip()
    return order_id, item_ids, reason


def _tool_message(call_id: str, name: str, result: dict) -> dict:
    return {
        "role": "tool",
        "tool_call_id": call_id,
        "name": name,
        "content": json.dumps(result, default=str)[:12000],
    }


# --------------------------------------------------------------- providers
def complete_chat(
    settings: Settings,
    messages: list[dict],
    tools: list[dict],
    timeout_s: float = 20.0,
) -> LLMReply:
    if settings.llm_provider == "openai":
        return _openai(settings, messages, tools, timeout_s)
    if settings.llm_provider == "anthropic":
        return _anthropic(settings, messages, tools, timeout_s)
    if settings.llm_provider == "google":
        return _google(settings, messages, tools, timeout_s)
    raise ValueError(f"Unsupported LLM provider: {settings.llm_provider!r}")


def _openai(
    settings: Settings, messages: list[dict], tools: list[dict], timeout_s: float
) -> LLMReply:
    base = settings.llm_base_url or "https://api.openai.com/v1"
    model = settings.llm_model or DEFAULT_MODELS["openai"]
    response = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": _openai_messages(messages),
            "tools": tools,
            "tool_choice": "auto",
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    message = response.json()["choices"][0]["message"]
    calls = []
    for entry in message.get("tool_calls") or []:
        function = entry.get("function") or {}
        calls.append(
            {
                "id": entry.get("id"),
                "name": function.get("name"),
                "arguments": function.get("arguments") or "{}",
            }
        )
    return LLMReply(text=(message.get("content") or "").strip(), tool_calls=calls)


def _openai_messages(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    for message in messages:
        role = message.get("role")
        if role == "assistant":
            entry: dict = {"role": "assistant", "content": message.get("content")}
            calls = message.get("tool_calls") or []
            if calls:
                entry["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("arguments") or {}),
                        },
                    }
                    for call in calls
                ]
            converted.append(entry)
        elif role == "tool":
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.get("tool_call_id"),
                    "content": message.get("content") or "",
                }
            )
        else:
            converted.append({"role": role, "content": message.get("content") or ""})
    return converted


def _anthropic(
    settings: Settings, messages: list[dict], tools: list[dict], timeout_s: float
) -> LLMReply:
    model = settings.llm_model or DEFAULT_MODELS["anthropic"]
    system = "\n\n".join(
        message.get("content") or ""
        for message in messages
        if message.get("role") == "system"
    )
    anthropic_tools = [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "input_schema": tool["function"]["parameters"],
        }
        for tool in tools
    ]
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.llm_api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 1024,
            "system": system,
            "messages": _anthropic_messages(messages),
            "tools": anthropic_tools,
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    texts: list[str] = []
    calls: list[dict] = []
    for block in response.json().get("content", []):
        if block.get("type") == "text":
            texts.append(block.get("text") or "")
        elif block.get("type") == "tool_use":
            calls.append(
                {
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": block.get("input") or {},
                }
            )
    return LLMReply(text="".join(texts).strip(), tool_calls=calls)


def _anthropic_messages(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "system":
            index += 1
            continue
        if role == "tool":
            blocks = []
            while index < len(messages) and messages[index].get("role") == "tool":
                blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": messages[index].get("tool_call_id"),
                        "content": messages[index].get("content") or "",
                    }
                )
                index += 1
            converted.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            blocks = []
            if message.get("content"):
                blocks.append({"type": "text", "text": message["content"]})
            for call in message.get("tool_calls") or []:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["name"],
                        "input": call.get("arguments") or {},
                    }
                )
            converted.append({"role": "assistant", "content": blocks or ""})
        else:
            converted.append({"role": "user", "content": message.get("content") or ""})
        index += 1
    return converted


def _google(
    settings: Settings, messages: list[dict], tools: list[dict], timeout_s: float
) -> LLMReply:
    model = settings.llm_model or DEFAULT_MODELS["google"]
    google_tools = [
        {
            "functionDeclarations": [
                {
                    "name": tool["function"]["name"],
                    "description": tool["function"]["description"],
                    "parameters": tool["function"]["parameters"],
                }
                for tool in tools
            ]
        }
    ]
    system = "\n\n".join(
        message.get("content") or ""
        for message in messages
        if message.get("role") == "system"
    )
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent",
        params={"key": settings.llm_api_key},
        json={
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": _google_contents(messages),
            "tools": google_tools,
            "generationConfig": {"temperature": 0},
        },
        timeout=timeout_s,
    )
    response.raise_for_status()
    texts: list[str] = []
    calls: list[dict] = []
    candidates = response.json().get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    for part in parts:
        if "text" in part:
            texts.append(part.get("text") or "")
        if "functionCall" in part:
            function = part["functionCall"]
            calls.append(
                {
                    "id": f"call_{uuid.uuid4().hex[:8]}",
                    "name": function.get("name"),
                    "arguments": function.get("args") or {},
                }
            )
    return LLMReply(text="".join(texts).strip(), tool_calls=calls)


def _google_contents(messages: list[dict]) -> list[dict]:
    converted: list[dict] = []
    index = 0
    while index < len(messages):
        message = messages[index]
        role = message.get("role")
        if role == "system":
            index += 1
            continue
        if role == "tool":
            parts = []
            while index < len(messages) and messages[index].get("role") == "tool":
                entry = messages[index]
                try:
                    payload = json.loads(entry.get("content") or "{}")
                except Exception:
                    payload = {"result": entry.get("content") or ""}
                parts.append(
                    {
                        "functionResponse": {
                            "name": entry.get("name") or "",
                            "response": {"result": payload},
                        }
                    }
                )
                index += 1
            converted.append({"role": "user", "parts": parts})
            continue
        if role == "assistant":
            parts = []
            if message.get("content"):
                parts.append({"text": message["content"]})
            for call in message.get("tool_calls") or []:
                parts.append(
                    {
                        "functionCall": {
                            "name": call["name"],
                            "args": call.get("arguments") or {},
                        }
                    }
                )
            converted.append({"role": "model", "parts": parts})
        else:
            converted.append(
                {"role": "user", "parts": [{"text": message.get("content") or ""}]}
            )
        index += 1
    return converted
