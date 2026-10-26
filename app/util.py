"""Small date/number/text helpers shared across the app."""

from __future__ import annotations

import re
from datetime import date, datetime


def parse_date(value) -> date | None:
    """Parse 'YYYY-MM-DD' or an ISO timestamp. Returns None when empty/bad."""
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


def format_date(value) -> str:
    """Format a date as '3 July 2026' (day without a leading zero)."""
    parsed = parse_date(value)
    if parsed is None:
        return ""
    return f"{parsed.day} {parsed.strftime('%B')} {parsed.year}"


def days_between(start: date, end: date) -> int:
    return (end - start).days


def trim_sentences(text: str, max_chars: int = 420) -> str:
    """Collapse whitespace and keep whole sentences up to roughly max_chars."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if len(clean) <= max_chars:
        return clean
    cut = clean[:max_chars]
    stop = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("! "))
    if stop >= 80:
        return cut[: stop + 1].strip()
    return cut.rsplit(" ", 1)[0].strip() + "…"


def money(amount: float) -> str:
    return f"₹{amount:,.0f}"
