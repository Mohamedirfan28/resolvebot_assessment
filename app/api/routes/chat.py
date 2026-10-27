"""POST /chat endpoint with the HTTP error contract from the brief."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.db import repository
from app.schemas.chat import ChatRequest
from app.services.sessions import SessionConflict
from app.services.tools import ToolContext

logger = logging.getLogger("resolvebot.api")
router = APIRouter(tags=["chat"])


def require_customer_id(
    x_customer_id: str | None = Header(default=None, alias="X-Customer-Id"),
) -> str:
    """Expose the header in OpenAPI and reject a missing value with 401."""
    if not x_customer_id or not x_customer_id.strip():
        raise HTTPException(
            status_code=401, detail="Missing X-Customer-Id header"
        )
    return x_customer_id.strip()


def _process(
    *,
    settings,
    index,
    sessions,
    assistant,
    customer_id: str,
    session_id: str,
    message: str,
) -> tuple[int, dict]:
    """Run one chat turn in a worker thread (SQLite is blocking)."""
    conn = None
    try:
        conn = repository.connect(settings.db_path)
        if not repository.customer_exists(conn, customer_id):
            return 401, {"detail": "Unknown X-Customer-Id"}

        try:
            session = sessions.get_or_create(session_id, customer_id)
        except SessionConflict:
            return 403, {"detail": "This session belongs to another customer"}

        ctx = ToolContext(
            conn,
            settings,
            index,
            customer_id,
            max_tool_calls=settings.max_tool_calls,
        )
        result = assistant.handle(ctx, session, message)
        return 200, {
            "request_id": str(uuid.uuid4()),
            "answer": result.answer,
            "route": result.route,
            "citations": result.citations,
            "tool_calls": result.tool_calls,
            "pending_confirmation": result.pending_confirmation,
            "session_id": session_id,
        }
    except Exception:
        logger.exception("chat request failed")
        return 200, {
            "request_id": str(uuid.uuid4()),
            "answer": (
                "Sorry, something went wrong. Please try again or email "
                "support@shopsphere.example.com."
            ),
            "route": "answer",
            "citations": [],
            "tool_calls": [],
            "pending_confirmation": False,
            "session_id": session_id,
        }
    finally:
        if conn is not None:
            conn.close()


@router.post(
    "/chat",
    summary="Send a message to ResolveBot",
    response_description="A reply with route, citations and tool calls",
)
async def chat(
    request: Request,
    payload: ChatRequest,
    customer_id: str = Depends(require_customer_id),
):
    status, response_payload = await run_in_threadpool(
        _process,
        settings=request.app.state.settings,
        index=request.app.state.policy_index,
        sessions=request.app.state.sessions,
        assistant=request.app.state.assistant,
        customer_id=customer_id,
        session_id=payload.session_id,
        message=payload.message,
    )
    return JSONResponse(status_code=status, content=response_payload)
