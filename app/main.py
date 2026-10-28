"""Application factory and lifespan wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.requests import Request

from app import __version__
from app.api.routes import chat, health
from app.core.config import get_settings
from app.core.logging import configure_logging
from app.services.assistant import Assistant
from app.services.policy_search import PolicyIndex
from app.services.sessions import SessionStore

logger = logging.getLogger("resolvebot")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)
    index = PolicyIndex(
        settings.policies_path,
        settings.manifest_path,
        settings.app_today,
    )
    app.state.settings = settings
    app.state.policy_index = index
    app.state.sessions = SessionStore()
    app.state.assistant = Assistant(
        max_tool_calls=settings.max_tool_calls,
        settings=settings,
    )
    logger.info(
        "ResolveBot started: %d policy sections, APP_TODAY=%s, LLM=%s",
        len(index),
        settings.app_today.isoformat(),
        settings.llm_provider if settings.llm_configured else "none",
    )
    yield
    logger.info("ResolveBot stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="ResolveBot",
        docs_url="/docs/",
        description=(
            "Customer-support chat assistant for ShopSphere. It looks up the "
            "authenticated customer's orders, answers policy questions with "
            "citations, checks return/warranty eligibility, and creates return "
            "requests or support tickets only after the customer confirms."
        ),
        version=__version__,
        lifespan=lifespan,
        contact={"name": "ShopSphere Support", "email": "support@shopsphere.example.com"},
    )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url="/docs/")

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # The brief requires 400 for an invalid chat body, not FastAPI's 422.
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid request body"},
        )

    app.include_router(health.router)
    app.include_router(chat.router)
    return app


app = create_app()
