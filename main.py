"""FastAPI entry point: POST /chat on port 8000.

Run with:
    uvicorn main:app --port 8000
or:
    python main.py
"""

from __future__ import annotations

import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app import repository
from app.assistant import Assistant
from app.config import Settings, get_settings
from app.llm import polish
from app.policy_search import PolicyIndex
from app.schemas import ChatRequest, ChatResponse, ErrorResponse
from app.sessions import SessionConflict, SessionStore
from app.tools import ToolContext

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    index = PolicyIndex(
        settings.policies_path, settings.manifest_path, settings.app_today
    )
    app.state.settings = settings
    app.state.index = index
    app.state.sessions = SessionStore()
    app.state.assistant = Assistant(
        max_tool_calls=settings.max_tool_calls, settings=settings
    )
    logging.getLogger("resolvebot").info(
        "Loaded %d policy paragraphs; APP_TODAY=%s", len(index.chunks), settings.app_today
    )
    yield


app = FastAPI(title="ResolveBot", version="1.0.0", lifespan=lifespan)


def _json(status: int, payload: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=payload)


def _error(status: int, message: str) -> JSONResponse:
    return _json(status, {"detail": message})


def _process(
    settings: Settings,
    index: PolicyIndex,
    sessions: SessionStore,
    assistant: Assistant,
    customer_id: str,
    session_id: str,
    message: str,
) -> tuple[int, dict]:
    conn: sqlite3.Connection | None = None
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

        if settings.llm_polish and not settings.llm_agent and result.route == "answer":
            result.answer = polish(settings, message, result.answer)

        return 200, {
            "request_id": str(uuid.uuid4()),
            "answer": result.answer,
            "route": result.route,
            "citations": result.citations,
            "tool_calls": result.tool_calls,
            "pending_confirmation": result.pending_confirmation,
        }
    except Exception:
        logging.getLogger("resolvebot").exception("chat request failed")
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
        }
    finally:
        if conn is not None:
            conn.close()


@app.post(
    "/chat",
    response_model=ChatResponse,
    response_model_exclude_none=True,
    responses={400: {"model": ErrorResponse}, 401: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
)
async def chat(request: Request):
    customer_id = request.headers.get("X-Customer-Id")
    if not customer_id or not customer_id.strip():
        return _error(401, "Missing X-Customer-Id header")
    customer_id = customer_id.strip()

    try:
        body = await request.json()
    except Exception:
        return _error(400, "Body must be valid JSON")

    try:
        payload = ChatRequest.model_validate(body)
    except ValidationError:
        return _error(
            400, "session_id and message are required non-empty strings"
        )

    status, result = await run_in_threadpool(
        _process,
        request.app.state.settings,
        request.app.state.index,
        request.app.state.sessions,
        request.app.state.assistant,
        customer_id,
        payload.session_id,
        payload.message,
    )
    if status == 200:
        return result
    return _json(status, result)


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
