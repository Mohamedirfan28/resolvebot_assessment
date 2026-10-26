"""Optional LLM answer polish.

The planner already decides every fact and every action. If LLM_POLISH=1 and a
provider key is configured, the model is only allowed to rephrase an `answer`
route reply. Any provider error falls back to the deterministic text.
"""

from __future__ import annotations

import logging

import httpx

from .config import Settings

logger = logging.getLogger("resolvebot.llm")

DEFAULT_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-3-5-haiku-latest",
    "google": "gemini-1.5-flash",
}

SYSTEM_PROMPT = (
    "You rewrite a customer-support reply for ShopSphere. Keep every fact, "
    "date, amount, reference, policy quote and error message exactly as given. "
    "Do not add information, do not promise anything, do not change a question "
    "into a statement. Return only the rewritten reply, in at most two short "
    "paragraphs."
)


def polish(settings: Settings, customer_message: str, draft: str) -> str:
    if not settings.llm_polish or not settings.llm_enabled:
        return draft
    try:
        if settings.llm_provider == "openai":
            return _openai(settings, customer_message, draft)
        if settings.llm_provider == "anthropic":
            return _anthropic(settings, customer_message, draft)
        if settings.llm_provider == "google":
            return _google(settings, customer_message, draft)
    except Exception:
        logger.exception("LLM polish failed; using deterministic answer")
    return draft


def _user_prompt(customer_message: str, draft: str) -> str:
    return (
        f"Customer message:\n{customer_message}\n\n"
        f"Draft reply (facts are fixed):\n{draft}"
    )


def _openai(settings: Settings, message: str, draft: str) -> str:
    base = settings.llm_base_url or "https://api.openai.com/v1"
    model = settings.llm_model or DEFAULT_MODELS["openai"]
    response = httpx.post(
        f"{base.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {settings.llm_api_key}"},
        json={
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": _user_prompt(message, draft)},
            ],
        },
        timeout=20,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip() or draft


def _anthropic(settings: Settings, message: str, draft: str) -> str:
    model = settings.llm_model or DEFAULT_MODELS["anthropic"]
    response = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": settings.llm_api_key,
            "anthropic-version": "2023-06-01",
        },
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 400,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": _user_prompt(message, draft)}],
        },
        timeout=20,
    )
    response.raise_for_status()
    blocks = response.json().get("content", [])
    return "".join(
        block.get("text", "") for block in blocks if block.get("type") == "text"
    ).strip() or draft


def _google(settings: Settings, message: str, draft: str) -> str:
    model = settings.llm_model or DEFAULT_MODELS["google"]
    response = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        params={"key": settings.llm_api_key},
        json={
            "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
            "contents": [
                {"role": "user", "parts": [{"text": _user_prompt(message, draft)}]}
            ],
            "generationConfig": {"temperature": 0},
        },
        timeout=20,
    )
    response.raise_for_status()
    candidates = response.json().get("candidates", [])
    if not candidates:
        return draft
    parts = candidates[0].get("content", {}).get("parts", [])
    return "".join(part.get("text", "") for part in parts).strip() or draft
