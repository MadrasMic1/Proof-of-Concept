# tests/test_itsm_worker.py
"""Phase 18: the ITSM loops of the worker (rolled-back test database, fake GLPI and sync)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import FixedClock
from app.itsm.errors import ItsmRejected, ItsmUnavailable
from app.itsm.snapshot import ChangedTicket
from app.itsm.sync import Outcome, SyncResult
from app.itsm.worker import FAILED, RECONCILE_JOB, RETRY, ItsmWorker, ItsmWorkerConfig
from app.models import ItsmInbox, ItsmSyncState

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)


class FakeSync:
    """Stands in for app.itsm.sync.sync_ticket: answers per ticket id."""

    def __init__(self, answers: dict[int, object] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[int, bool]] = []

    async def __call__(self, factory, client, ticket_id, *, now, api_username, quiet=False):
        self.calls.append((ticket_id, quiet))
        answer = self.answers.get(ticket_id, Outcome.DONE)
        if isinstance(answer, Exception):
            raise answer
        warnings = ["GLPI says In Progress but RIC is Closed"] if answer == Outcome.CONFLICT else []
        return SyncResult(ticket_id, str(answer), warnings=warnings)


class FakeGlpi:
    def __init__(self, changed: list[ChangedTicket] | Exception) -> None:
        self.changed = changed
        self.asked: list[datetime] = []

    async def changed_since(self, after: datetime) -> list[ChangedTicket]:
        self.asked.append(after)
        if isinstance(self.changed, Exception):
            raise self.changed
        return self.changed


@pytest.fixture
async def factory(db_session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Worker sessions on the test's connection (commits are savepoints). The queue and the
    job state start empty inside the rolled-back transaction."""
    await db_session.execute(delete(ItsmInbox))
    await db_session.execute(delete(ItsmSyncState))
    await db_session.flush()
    return async_sessionmaker(
        bind=db_session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


def make(factory, sync=None, client=None, clock=None, **config) -> ItsmWorker:
    values = {"max_attempts": 3}
    values.update(config)
    return ItsmWorker(
        factory,
        client or FakeGlpi([]),
        clock or FixedClock(NOW),
        ItsmWorkerConfig(**values),
        sync=sync or FakeSync(),
    )


async def add_row(session: AsyncSession, ticket: int, source: str = "webhook", **values) -> int:
    row = ItsmInbox(glpi_ticket_id=ticket, source=source, available_at=NOW - timedelta(minutes=1))
    for name, value in values.items():
        setattr(row, name, value)
    session.add(row)
    await session.flush()
    return row.id


async def row_of(session: AsyncSession, row_id: int) -> ItsmInbox:
    return await session.get(ItsmInbox, row_id, populate_existing=True)


# --- inbox loop -----------------------------------------------------------------------


async def test_rows_are_synced_and_finished(db_session, factory) -> None:
    link = await add_row(db_session, 980001, "link")
    hook = await add_row(db_session, 980002)
    clash = await add_row(db_session, 980003)
    sync = FakeSync({980003: Outcome.CONFLICT})
    worker = make(factory, sync)
    outcomes = [(await worker.process_next()).outcome for _ in range(3)]
    assert outcomes == ["done", "done", "conflict"]
    assert await worker.process_next() is None
    assert sync.calls == [(980001, True), (980002, False), (980003, False)]  # link rows: quiet
    assert (await row_of(db_session, link)).status == "done"
    assert (await row_of(db_session, hook)).processed_at == NOW
    conflict = await row_of(db_session, clash)
    assert (conflict.status, conflict.last_error) == (
        "conflict",
        "GLPI says In Progress but RIC is Closed",
    )


async def test_unavailable_is_retried_then_failed(db_session, factory) -> None:
    row_id = await add_row(db_session, 980011)
    clock = FixedClock(NOW)
    worker = make(factory, FakeSync({980011: ItsmUnavailable("down", status=503)}), clock=clock)
    first = await worker.process_next()
    assert (first.outcome, first.unavailable) == (RETRY, True)
    row = await row_of(db_session, row_id)
    assert (row.status, row.attempts) == ("pending", 1)
    assert row.available_at == NOW + timedelta(seconds=30)
    assert "ItsmUnavailable" in row.last_error
    assert await worker.process_next() is None  # not due yet
    clock.advance(timedelta(seconds=30))
    assert (await worker.process_next()).outcome == RETRY
    assert (await row_of(db_session, row_id)).available_at == clock.now() + timedelta(minutes=2)
    clock.advance(timedelta(minutes=2))
    assert (await worker.process_next()).outcome == FAILED  # max_attempts=3
    row = await row_of(db_session, row_id)
    assert (row.status, row.attempts) == ("failed", 3)


async def test_rejected_fails_at_once_and_unexpected_is_retried(db_session, factory) -> None:
    rejected = await add_row(db_session, 980021)
    broken = await add_row(db_session, 980022)
    sync = FakeSync({980021: ItsmRejected("bad token", status=401), 980022: KeyError("x")})
    worker = make(factory, sync)
    assert (await worker.process_next()).outcome == FAILED
    assert (await row_of(db_session, rejected)).status == "failed"
    result = await worker.process_next()
    assert (result.outcome, result.unavailable) == (RETRY, False)
    assert (await row_of(db_session, broken)).status == "pending"


async def test_retry_closes_the_row_when_a_newer_delivery_waits(db_session, factory) -> None:
    old = await add_row(db_session, 980031)
    sync = FakeSync({980031: ItsmUnavailable("down")})
    worker = make(factory, sync)

    async def newer_arrives(*args, **kwargs):
        await add_row(db_session, 980031)  # a webhook while the row is processing
        raise ItsmUnavailable("down")

    worker._sync = newer_arrives
    result = await worker.process_next()
    assert result.outcome == RETRY and "newer row" in result.detail
    assert (await row_of(db_session, old)).status == "done"


async def test_startup_releases_processing_rows_and_drain(db_session, factory) -> None:
    stuck = await add_row(db_session, 980041, status="processing", attempts=1)
    await add_row(db_session, 980042)
    later = await add_row(db_session, 980043)
    await db_session.execute(
        ItsmInbox.__table__.update()
        .where(ItsmInbox.id == later)
        .values(available_at=NOW + timedelta(hours=1))
    )
    stats = await make(factory).drain()
    assert dict(stats.outcomes) == {"done": 2} and stats.still_pending == 1
    assert (await row_of(db_session, stuck)).status == "done"


async def test_drain_stops_when_glpi_is_down(db_session, factory) -> None:
    await add_row(db_session, 980051)
    await add_row(db_session, 980052)
    sync = FakeSync({980051: ItsmUnavailable("down"), 980052: ItsmUnavailable("down")})
    stats = await make(factory, sync).drain()
    assert dict(stats.outcomes) == {RETRY: 1} and stats.still_pending == 2
    assert len(sync.calls) == 1


async def test_purge_keeps_recent_failed_and_conflict_rows(db_session, factory) -> None:
    old = NOW - timedelta(days=15)
    ids = [
        await add_row(db_session, 980061, status="done", processed_at=old),
        await add_row(db_session, 980062, status="ignored", processed_at=old),
        await add_row(db_session, 980063, status="done", processed_at=NOW - timedelta(days=1)),
        await add_row(db_session, 980064, status="failed", processed_at=old),
        await add_row(db_session, 980065, status="conflict", processed_at=old),
    ]
    assert await make(factory).purge() == 2
    left = set((await db_session.scalars(select(ItsmInbox.id))).all())
    assert left == set(ids[2:])


# --- reconciliation ---------------------------------------------------------------------


async def set_watermark(session: AsyncSession, watermark: datetime | None, next_run=NOW) -> None:
    session.add(
        ItsmSyncState(
            job_name=RECONCILE_JOB, interval_seconds=900, next_run_at=next_run, watermark=watermark
        )
    )
    await session.flush()


async def state(session: AsyncSession) -> ItsmSyncState:
    return await session.get(ItsmSyncState, RECONCILE_JOB, populate_existing=True)


async def test_reconcile_queues_changed_tickets_and_moves_the_watermark(db_session, factory):
    mark = NOW - timedelta(hours=1)
    await set_watermark(db_session, mark)
    await add_row(db_session, 980072)  # already pending: not queued twice
    newest = NOW - timedelta(minutes=2)
    glpi = FakeGlpi(
        [
            ChangedTicket(980071, NOW - timedelta(minutes=30)),
            ChangedTicket(980072, NOW - timedelta(minutes=20)),
            ChangedTicket(980073, newest),
        ]
    )
    worker = make(factory, client=glpi)
    result = await worker.reconcile()
    assert (result.ok, result.changed, result.queued) == (True, 3, 2)
    assert glpi.asked == [mark - timedelta(minutes=5)]
    job = await state(db_session)
    assert job.watermark == newest and job.next_run_at == NOW + timedelta(minutes=15)
    assert (job.last_success_at, job.consecutive_failures, job.last_error) == (NOW, 0, None)
    assert job.details["queued"] == 2 and job.details["alreadyPending"] == 1
    rows = (
        await db_session.scalars(select(ItsmInbox).where(ItsmInbox.source == "reconcile"))
    ).all()
    assert sorted(r.glpi_ticket_id for r in rows) == [980071, 980073]
    assert rows[0].events[0]["event"] == "reconcile"
    assert await worker.reconcile() is None  # not due again for 15 minutes
    assert (await worker.reconcile(force=True)).ok


async def test_reconcile_never_moves_the_watermark_back_or_on_failure(db_session, factory):
    mark = NOW - timedelta(minutes=10)
    await set_watermark(db_session, mark)
    quiet = await make(factory, client=FakeGlpi([])).reconcile()
    assert quiet.ok and quiet.watermark == mark
    failing = make(factory, client=FakeGlpi(ItsmUnavailable("down")))
    result = await failing.reconcile(force=True)
    assert not result.ok and "ItsmUnavailable" in result.error
    job = await state(db_session)
    assert (job.watermark, job.consecutive_failures) == (mark, 1)
    assert "down" in job.last_error


async def test_reconcile_waits_for_itsm_link(db_session, factory) -> None:
    glpi = FakeGlpi([ChangedTicket(980081, NOW)])
    result = await make(factory, client=glpi).reconcile()  # creates the job row itself
    assert result.skipped and "itsm_link" in result.error
    assert glpi.asked == []
    job = await state(db_session)
    assert job.watermark is None and job.next_run_at == NOW + timedelta(minutes=15)


def test_config_from_settings() -> None:
    from app.core.config import Settings

    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        itsm_sync_retry_delays_seconds="10, 60",
    )
    config = ItsmWorkerConfig.from_settings(settings)
    assert config.retry_delays == (timedelta(seconds=10), timedelta(minutes=1))
    assert config.retry_delay(5) == timedelta(minutes=1)
    assert config.reconcile_interval == timedelta(minutes=15) and config.max_attempts == 8
    with pytest.raises(ValueError, match="ITSM_SYNC_RETRY_DELAYS_SECONDS"):
        ItsmWorkerConfig.from_settings(
            settings.model_copy(update={"itsm_sync_retry_delays_seconds": "soon"})
        )
