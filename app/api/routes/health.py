"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health", summary="Service health")
async def health(request: Request) -> dict:
    state = request.app.state
    return {
        "status": "ok",
        "policy_sections": len(state.policy_index),
        "app_today": state.settings.app_today.isoformat(),
    }
