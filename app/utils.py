"""Date and string helpers used by the rules and policy index."""

from __future__ import annotations

import re
from datetime import date, datetime


def parse_date(value: object) -> date | None:
    """Parse a date, or the date part of an ISO timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def format_date(value: object) -> str:
    """Format as '3 July 2026' (no leading zero on the day)."""
    parsed = parse_date(value)
    if parsed is None:
        return ""
    return f"{parsed.day} {parsed.strftime('%B')} {parsed.year}"


def trim_text(text: str, max_chars: int = 420) -> str:
    """Collapse whitespace and keep whole sentences within a rough limit."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars]
    stop = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "))
    if stop >= 80:
        return cut[: stop + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + "..."
