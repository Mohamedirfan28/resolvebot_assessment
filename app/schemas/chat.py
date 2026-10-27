"""Pydantic request/response schemas for the chat API."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=4000)


class Citation(BaseModel):
    doc_id: str
    section: str


class ToolCall(BaseModel):
    name: str
    args: dict = Field(default_factory=dict)
    status: Literal["success", "error", "pending_confirmation"]
    error_code: str | None = None


class ChatResponse(BaseModel):
    request_id: str
    answer: str
    route: Literal["answer", "clarify", "no_answer", "out_of_scope"]
    citations: list[Citation] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    pending_confirmation: bool = False
    session_id: str


class ErrorResponse(BaseModel):
    detail: str
