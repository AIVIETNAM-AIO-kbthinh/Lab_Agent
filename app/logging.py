"""structlog JSON ra stdout, kèm request_id / op_id trong contextvars.

Không log nội dung chat, CV hay SĐT: chỉ log ID và độ dài.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog

_SECRET_KEYS = {"api_key", "password", "token", "secret", "authorization", "cookie", "refresh_token"}


def _redact(_: Any, __: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    for k in list(event_dict):
        if any(s in k.lower() for s in _SECRET_KEYS):
            event_dict[k] = "***"
    return event_dict


def setup_logging(level: str = "INFO", json: bool = True) -> None:
    lvl = getattr(logging, level.upper(), logging.INFO)
    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _redact,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = (
        structlog.processors.JSONRenderer(ensure_ascii=False) if json else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[*processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(lvl),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    logging.basicConfig(level=lvl, stream=sys.stdout, format="%(message)s")
    for noisy in ("httpx", "httpx2", "googleapiclient.discovery_cache", "urllib3", "httpcore", "httpcore2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def bind(**kw: Any) -> None:
    structlog.contextvars.bind_contextvars(**kw)


def clear() -> None:
    structlog.contextvars.clear_contextvars()
