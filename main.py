"""One-command entry point: ``uvicorn main:app --port 8000``."""

from __future__ import annotations

import uvicorn

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.main import app

__all__ = ["app"]


if __name__ == "__main__":
    settings = get_settings()
    configure_logging(settings.log_level)
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
