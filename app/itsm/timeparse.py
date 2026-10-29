# app/itsm/timeparse.py
"""Date/time conversion between GLPI and RIC.

    API V2 returns ISO 8601 with an offset:  2026-10-28T11:02:02+00:00
    API V1 returns text without a zone:      2026-10-28 11:02:02   (in GLPI_TIMEZONE)
    V2 filters expect the V1 text form:      date_mod=gt=2026-10-28 11:02:02

RIC stores every time as timezone-aware UTC. Empty values (None, "",
"0000-00-00 00:00:00") become None.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

GLPI_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def parse_glpi_time(value: object, tz: ZoneInfo) -> datetime | None:
    """A GLPI date/time (V1 text or V2 ISO) as UTC. Values without an offset are read in tz."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"not a GLPI date/time: {value!r}")
    text = value.strip()
    if not text or text.startswith("0000"):
        return None
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def to_glpi_text(value: datetime, tz: ZoneInfo) -> str:
    """A timezone-aware time as GLPI date/time text in tz (used in V2 filters)."""
    if value.tzinfo is None:
        raise ValueError("times sent to GLPI must be timezone-aware")
    return value.astimezone(tz).strftime(GLPI_TEXT_FORMAT)
