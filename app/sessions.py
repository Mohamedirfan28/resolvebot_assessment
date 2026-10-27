"""In-memory chat sessions.

Each session remembers its messages, one waiting proposal and one "soft offer"
(the answer to a question like "can I return it?"). A session belongs to the
customer who started it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock


class SessionConflict(Exception):
    """The session_id belongs to another customer."""


@dataclass
class Proposal:
    kind: str                      # return | warranty | cancellation
    order_id: str
    item_ids: list[str] = field(default_factory=list)
    item_names: list[str] = field(default_factory=list)
    reason: str | None = None
    summary: str = ""
    citations: list[dict] = field(default_factory=list)
    tool_name: str = ""            # create_return_request / create_support_ticket
    args: dict = field(default_factory=dict)
    answer_hint: str = ""


@dataclass
class Session:
    session_id: str
    customer_id: str
    messages: list[dict] = field(default_factory=list)
    pending: Proposal | None = None
    offer: Proposal | None = None
    clarification: dict | None = None

    def remember(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})
        del self.messages[:-40]


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = Lock()

    def get_or_create(self, session_id: str, customer_id: str) -> Session:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = Session(session_id=session_id, customer_id=customer_id)
                self._sessions[session_id] = session
            elif session.customer_id != customer_id:
                raise SessionConflict
            return session

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()
