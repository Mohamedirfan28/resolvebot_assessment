"""Shared result type, kept separate so app.agent and app.assistant can both use
it without a circular import."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ChatResult:
    answer: str
    route: str = "answer"
    citations: list[dict] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)
    pending_confirmation: bool = False
