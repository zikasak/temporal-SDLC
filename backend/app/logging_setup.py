"""Logging configuration shared by the API and the worker."""

import logging

from app.config import LOG_LEVEL

_configured = False


def configure_logging() -> None:
    global _configured
    if _configured:
        return
    logging.basicConfig(
        level=LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )
    for name in ("httpx", "httpx2", "httpcore", "httpcore2", "openai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured = True
