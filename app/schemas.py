"""Pydantic request and response schemas for the public chat API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    """Body of ``POST /chat``. The customer identity comes from the header."""

    model_config = ConfigDict(extra="ignore")

    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)

    @field_validator("session_id", "message")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class Citation(BaseModel):
    doc_id: str
    section: str


class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    status: str
    error_code: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    answer: str
    route: Literal["answer", "clarify", "no_answer", "out_of_scope"]
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    pending_confirmation: bool = False


class ErrorResponse(BaseModel):
    detail: str
