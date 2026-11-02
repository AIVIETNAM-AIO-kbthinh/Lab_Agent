"""Chạy coroutine ở cấp tiến trình. psycopg async không chạy trên ProactorEventLoop (Windows)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any, TypeVar

T = TypeVar("T")


def loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def run(coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro, loop_factory=loop_factory)
