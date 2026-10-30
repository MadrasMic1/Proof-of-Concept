# tests/test_itsm_status.py
"""Phase 20: sync status and recovery helpers (rolled-back test database, no GLPI)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.itsm.resync import (
    conflict_rows,
    linked_ticket_ids,
    queue_tickets,
    retry_failed,
    start_watermark,
    ticket_of,
)
from app.itsm.status import FAIL, INFO, OK, WARN, collect_status
from app.itsm.worker import RECONCILE_JOB
from app.models import ItsmInbox, ItsmSyncState, Store
from app.services import incidents as incident_svc
from tests.itsm_helpers import IT, build_itsm_fixture, new_incident

# Real "now": rows get updated_at = now() from the database.
NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
async def session(db_session: AsyncSession) -> AsyncSession:
    """The queue and the job state start empty inside the rolled-back transaction."""
    await db_session.execute(delete(ItsmInbox))
    await db_session.execute(delete(ItsmSyncState))
    await db_session.flush()
    return db_session


def levels(report, area: str) -> list[str]:
    return [check.level for check in report.checks if check.area == area]


def messages(report, area: str) -> str:
    return " | ".join(check.message for check in report.checks if check.area == area)


async def add_row(session: AsyncSession, ticket: int, **values) -> ItsmInbox:
    row = ItsmInbox(glpi_ticket_id=ticket, source=values.pop("source", "webhook"))
    for name, value in values.items():
        setattr(row, name, value)
    session.add(row)
    await session.flush()
    return row


async def add_state(session: AsyncSession, **values) -> None:
    state = ItsmSyncState(job_name=RECONCILE_JOB, interval_seconds=900, next_run_at=NOW)
    for name, value in values.items():
        setattr(state, name, value)
    session.add(state)
    await session.flush()


# --- status -----------------------------------------------------------------------------


async def test_nothing_set_up_is_a_failure(session, test_settings: Settings) -> None:
    report = await collect_status(session, now=NOW, settings=test_settings)
    assert levels(report, "reconcile") == [FAIL]
    assert "itsm_link" in messages(report, "reconcile")
    assert report.verdict == FAIL
    assert report.inbox.due == 0 and report.problems == []
    assert levels(report, "webhooks") == [INFO]
    assert levels(report, "journal") == [INFO]


async def test_healthy_reconcile_and_inbox_problems(session, test_settings: Settings) -> None:
    await add_state(
        session,
        watermark=NOW - timedelta(minutes=20),
        last_success_at=NOW - timedelta(minutes=5),
        consecutive_failures=0,
    )
    itsm = await build_itsm_fixture(session)
    store = await session.get(Store, itsm.store_a)
    store.glpi_location_id = 990900
    await session.flush()
    sent = NOW - timedelta(minutes=3)
    await add_row(
        session,
        990901,
        status="done",
        processed_at=NOW,
        received_at=sent + timedelta(seconds=45),
        events=[
            {"event": "ITILFollowup/new", "glpi_time": sent.isoformat(),
             "received_at": (sent + timedelta(seconds=45)).isoformat()},
        ],
    )  # fmt: skip
    await add_row(session, 990902, received_at=NOW - timedelta(minutes=20),
                  available_at=NOW - timedelta(minutes=20))  # fmt: skip
    await add_row(session, 990903, available_at=NOW + timedelta(minutes=10))
    await add_row(session, 990904, status="processing", updated_at=NOW - timedelta(hours=1))
    await add_row(session, 990905, status="failed", processed_at=NOW, last_error="ItsmRejected")
    await add_row(session, 990906, status="conflict", processed_at=NOW - timedelta(days=1),
                  last_error="GLPI says In Progress but RIC is Closed")  # fmt: skip
    await add_row(session, 990907, status="conflict", processed_at=NOW - timedelta(days=9))
    report = await collect_status(session, now=NOW, settings=test_settings)

    assert levels(report, "reconcile") == [OK]
    assert report.reconcile.watermark == NOW - timedelta(minutes=20)
    inbox = levels(report, "inbox")
    assert inbox.count(WARN) == 4  # old due row, stuck row, failed row, recent conflict
    assert report.inbox.due == 1 and report.inbox.waiting_retry == 1
    assert report.inbox.stuck_processing == 1
    assert report.inbox.by_status["pending"] == 2 and report.inbox.by_source == {"webhook": 2}
    problems = {(row.status, row.glpi_ticket_id) for row in report.problems}
    assert problems == {("failed", 990905), ("conflict", 990906)}  # 9-day-old one is not listed
    assert report.webhooks.deliveries_24h == 1
    assert report.webhooks.delay_median_seconds == 45.0
    assert report.verdict == WARN


@pytest.mark.parametrize(
    ("values", "level", "text"),
    [
        ({"watermark": None}, FAIL, "no watermark"),
        ({"last_success_at": None}, WARN, "never"),
        ({"last_success_at": NOW - timedelta(hours=1)}, WARN, "scripts.worker"),
        ({"consecutive_failures": 1, "last_error": "ItsmUnavailable"}, WARN, "last run failed"),
        ({"consecutive_failures": 3, "last_error": "ItsmUnavailable"}, FAIL, "3 failed runs"),
    ],
)
async def test_reconcile_levels(session, test_settings: Settings, values, level, text) -> None:
    state = {"watermark": NOW - timedelta(minutes=20), "last_success_at": NOW, **values}
    await add_state(session, **state)
    report = await collect_status(session, now=NOW, settings=test_settings)
    assert levels(report, "reconcile") == [level]
    assert text in messages(report, "reconcile")


async def test_status_route(client: AsyncClient) -> None:
    response = await client.get("/api/v1/admin/itsm/status")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] in ("OK", "WARN", "FAIL")
    assert {check["area"] for check in body["checks"]} >= {"reconcile", "inbox", "links"}
    assert set(body) >= {"checkedAt", "reconcile", "inbox", "problems", "webhooks", "links"}


# --- recovery ---------------------------------------------------------------------------


async def test_queue_tickets(session) -> None:
    assert await queue_tickets(session, [990911, 990912, 990911], now=NOW, quiet=True) == (2, 0)
    assert await queue_tickets(session, [990911], now=NOW) == (0, 1)
    sources = set((await session.scalars(select(ItsmInbox.source))).all())
    assert sources == {"link"}


async def test_retry_failed(session) -> None:
    first = await add_row(session, 990921, status="failed", processed_at=NOW, last_error="x")
    second = await add_row(session, 990921, status="failed", processed_at=NOW, last_error="y")
    other = await add_row(session, 990922, status="failed", processed_at=NOW, last_error="z")
    await add_row(session, 990922)  # already pending
    assert await retry_failed(session, now=NOW) == (1, 2)
    rows = {
        row.id: row
        for row in (
            await session.scalars(select(ItsmInbox).execution_options(populate_existing=True))
        ).all()
    }
    assert (rows[first.id].status, rows[first.id].attempts) == ("pending", 0)
    assert rows[first.id].processed_at is None
    assert rows[second.id].status == "done" and "superseded" in rows[second.id].last_error
    assert rows[other.id].status == "done"


async def test_conflict_rows_window(session) -> None:
    await add_row(session, 990931, status="conflict", processed_at=NOW, last_error="reopened")
    await add_row(session, 990932, status="conflict", processed_at=NOW - timedelta(days=8))
    rows = await conflict_rows(session, now=NOW)
    assert [(row.glpi_ticket_id, row.reason) for row in rows] == [(990931, "reopened")]


async def test_start_watermark(session) -> None:
    first = NOW - timedelta(minutes=1)
    assert await start_watermark(session, first, interval_seconds=900, now=NOW) == (None, first)
    state = await session.get(ItsmSyncState, RECONCILE_JOB)
    assert state.next_run_at == NOW and state.interval_seconds == 900
    assert await start_watermark(session, NOW, interval_seconds=900, now=NOW) == (first, first)
    assert await start_watermark(session, NOW, interval_seconds=900, now=NOW, force=True) == (
        first,
        NOW,
    )


async def test_linked_tickets(db_session: AsyncSession) -> None:
    itsm = await build_itsm_fixture(db_session)
    incident = await incident_svc.create_incident(
        db_session, IT, itsm.store_a, new_incident(), NOW, commit=False
    )
    incident.glpi_ticket_id = 990941
    await db_session.flush()
    assert 990941 in await linked_ticket_ids(db_session)
    assert await ticket_of(db_session, incident.number) == 990941
    assert await ticket_of(db_session, "INC09999999") is None
