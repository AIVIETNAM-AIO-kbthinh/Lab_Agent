"""Thời gian theo múi giờ lab. Test tiêm thời điểm bằng `set_now()` hoặc time-machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

_TZ = ZoneInfo("Asia/Ho_Chi_Minh")
_now_fn: Callable[[], datetime] | None = None


def configure(tz_name: str) -> None:
    global _TZ
    _TZ = ZoneInfo(tz_name)


def tz() -> ZoneInfo:
    return _TZ


def now() -> datetime:
    """Thời điểm hiện tại, có múi giờ của lab."""
    if _now_fn is not None:
        return _now_fn().astimezone(_TZ)
    return datetime.now(_TZ)


def utcnow() -> datetime:
    return now().astimezone(UTC)


def today() -> date:
    return now().date()


def set_now(fn: Callable[[], datetime] | None) -> None:
    global _now_fn
    _now_fn = fn
