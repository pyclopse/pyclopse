"""Centralised "current time" helper for pyclopse.

All timestamps in pyclopse use timezone-naive datetimes expressed in a single,
consistent timezone.  Call ``configure_timezone()`` once at startup (gateway
does this automatically); after that every call to ``now()`` returns the
current wall-clock time in the configured zone.

If no timezone is configured the system's local time is used — equivalent to
``datetime.now()`` with no tz argument.

Storage format: timezone-naive ISO strings (``2026-03-11T23:05:42.123456``).
The "wall clock in configured zone" approach avoids DST-awareness complexity
while remaining unambiguous as long as the configured zone is consistent.
"""

from __future__ import annotations

import logging
from datetime import datetime, date
from typing import Optional

_configured_tz = None  # None → system local; set to ZoneInfo by configure_timezone()

logger = logging.getLogger(__name__)


def configure_timezone(tz_name: Optional[str]) -> None:
    """Set the timezone used by ``now()``.  Pass ``None`` to use system local time.

    Should be called once at gateway startup before any timestamp is generated.
    Safe to call multiple times (e.g. on config reload).
    """
    global _configured_tz
    if not tz_name:
        _configured_tz = None
        logger.debug("pyclopse timezone: system local")
        return
    try:
        from zoneinfo import ZoneInfo
        _configured_tz = ZoneInfo(tz_name)
        logger.debug(f"pyclopse timezone: {tz_name}")
    except Exception as exc:
        logger.warning(f"Invalid timezone '{tz_name}': {exc} — falling back to system local")
        _configured_tz = None


def now() -> datetime:
    """Return the current datetime in the configured timezone (timezone-naive).

    This is the single source of truth for all pyclopse timestamps.
    Replace every ``datetime.now()`` / ``datetime.utcnow()`` call with this.
    """
    if _configured_tz is None:
        return datetime.now()
    return datetime.now(_configured_tz).replace(tzinfo=None)


def today() -> date:
    """Return today's date in the configured timezone."""
    return now().date()


def today_midnight() -> datetime:
    """Return midnight (00:00:00) of today in the configured timezone."""
    return datetime.combine(today(), datetime.min.time())
