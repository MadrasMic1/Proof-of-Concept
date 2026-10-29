# tests/test_itsm_timeparse.py
"""GLPI date/time text <-> timezone-aware UTC."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.itsm.timeparse import parse_glpi_time, to_glpi_text

UTC_ZONE = ZoneInfo("UTC")
KOLKATA = ZoneInfo("Asia/Kolkata")


def test_v2_iso_with_offset() -> None:
    value = parse_glpi_time("2026-10-28T11:02:02+00:00", UTC_ZONE)
    assert value == datetime(2026, 10, 28, 11, 2, 2, tzinfo=UTC)


def test_v2_offset_wins_over_glpi_timezone() -> None:
    value = parse_glpi_time("2026-10-28T16:32:02+05:30", UTC_ZONE)
    assert value == datetime(2026, 10, 28, 11, 2, 2, tzinfo=UTC)


def test_v1_text_is_read_in_glpi_timezone() -> None:
    assert parse_glpi_time("2026-10-28 11:02:02", UTC_ZONE) == datetime(
        2026, 10, 28, 11, 2, 2, tzinfo=UTC
    )
    assert parse_glpi_time("2026-10-28 16:32:02", KOLKATA) == datetime(
        2026, 10, 28, 11, 2, 2, tzinfo=UTC
    )


@pytest.mark.parametrize("value", [None, "", "   ", "0000-00-00 00:00:00"])
def test_empty_values(value: object) -> None:
    assert parse_glpi_time(value, UTC_ZONE) is None


def test_rejects_non_text() -> None:
    with pytest.raises(ValueError):
        parse_glpi_time(12345, UTC_ZONE)


def test_to_glpi_text() -> None:
    moment = datetime(2026, 10, 28, 11, 2, 2, tzinfo=UTC)
    assert to_glpi_text(moment, UTC_ZONE) == "2026-10-28 11:02:02"
    assert to_glpi_text(moment, KOLKATA) == "2026-10-28 16:32:02"
    with pytest.raises(ValueError):
        to_glpi_text(datetime(2026, 10, 28, 11, 2, 2), UTC_ZONE)
