# app/itsm/worker.py
"""The ITSM side of the worker (Stage 2, Phase 18).

Runs in the same process as the journal worker (scripts.worker), as two more loops:

    inbox loop      claims due itsm_inbox rows one by one and syncs the ticket
                    (app/itsm/sync.py). done / ignored / conflict finish the row;
                    GLPI unavailable -> retry later (ITSM_SYNC_RETRY_DELAYS_SECONDS)
                    and the loop pauses; GLPI rejected -> failed at once; anything
                    else -> retried, failed after ITSM_SYNC_MAX_ATTEMPTS.
    scheduler loop  every few seconds: runs the itsm_reconcile job when it is due
                    (itsm_sync_state.next_run_at, claimed with SKIP LOCKED so it runs
                    once even with two workers), and once an hour deletes finished
                    inbox rows older than ITSM_INBOX_RETENTION_DAYS.

itsm_reconcile (every ITSM_RECONCILE_INTERVAL_SECONDS): asks GLPI for incident
tickets changed after (watermark - ITSM_RECONCILE_OVERLAP_SECONDS) and queues each
one (source 'reconcile'; a ticket with a pending row is not queued twice). The
watermark moves only after a successful run, and only to the newest GLPI date_mod
seen (GLPI's clock, never RIC's). No watermark yet: the run is skipped until
scripts.itsm_link has set one. It catches what webhooks cannot: reassignments,
deliveries GLPI gave up on, changes made while the API was down.

The loops share the database through tables only: the sync writes pending journal
entries, which the journal loop (app/worker/runner.py) turns into alerts, actions
and notifications. Each loop catches its own errors, so one never stops the others.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock
from app.core.config import Settings
from app.db.session import session_scope
from app.itsm import inbox
from app.itsm.errors import ItsmRejected, ItsmUnavailable
from app.itsm.snapshot import ChangedTicket
from app.itsm.sync import Outcome, SyncResult, sync_ticket
from app.models import ItsmInbox, ItsmSyncState
from app.models.values import InboxSource, InboxStatus

logger = logging.getLogger("app.itsm.worker")

RECONCILE_JOB = "itsm_reconcile"
PURGE_EVERY_SECONDS = 3600.0
MAX_ERROR = 2000

# Outcome labels of process_next besides the sync outcomes (done / ignored / conflict).
RETRY = "retry"
FAILED = "failed"

QUEUE_SQL = text(
    """
    INSERT INTO itsm_inbox (glpi_ticket_id, source, events)
    VALUES (:ticket, :source, CAST(:events AS jsonb))
    ON CONFLICT (glpi_ticket_id) WHERE status = 'pending' DO NOTHING
    RETURNING id
    """
)
ENSURE_JOB_SQL = text(
    """
    INSERT INTO itsm_sync_state (job_name, interval_seconds, next_run_at)
    VALUES (:job, :interval, :now)
    ON CONFLICT (job_name) DO NOTHING
    """
)

SyncFunction = Callable[..., Awaitable[SyncResult]]


class ReconcileClient(Protocol):
    """The part of GlpiClient the reconciliation uses (the sync uses more)."""

    async def changed_since(self, after: datetime) -> list[ChangedTicket]: ...


@dataclass(frozen=True)
class ItsmWorkerConfig:
    poll_seconds: float = 5.0
    scheduler_poll_seconds: float = 10.0
    max_attempts: int = 8
    retry_delays: tuple[timedelta, ...] = (
        timedelta(seconds=30),
        timedelta(minutes=2),
        timedelta(minutes=10),
        timedelta(minutes=30),
    )
    reconcile_interval: timedelta = timedelta(minutes=15)
    reconcile_overlap: timedelta = timedelta(minutes=5)
    retention: timedelta = timedelta(days=14)
    api_username: str = "ric.api"

    @classmethod
    def from_settings(cls, settings: Settings) -> ItsmWorkerConfig:
        try:
            delays = tuple(
                timedelta(seconds=float(part))
                for part in settings.itsm_sync_retry_delays_seconds.split(",")
                if part.strip()
            )
        except ValueError as exc:
            raise ValueError(
                "ITSM_SYNC_RETRY_DELAYS_SECONDS must be comma-separated seconds, e.g. 30,120"
            ) from exc
        return cls(
            poll_seconds=settings.itsm_sync_poll_seconds,
            scheduler_poll_seconds=settings.itsm_scheduler_poll_seconds,
            max_attempts=settings.itsm_sync_max_attempts,
            retry_delays=delays or (timedelta(seconds=30),),
            reconcile_interval=timedelta(seconds=settings.itsm_reconcile_interval_seconds),
            reconcile_overlap=timedelta(seconds=settings.itsm_reconcile_overlap_seconds),
            retention=timedelta(days=settings.itsm_inbox_retention_days),
            api_username=settings.glpi_api_username,
        )

    def retry_delay(self, attempts: int) -> timedelta:
        """Delay before the next attempt after `attempts` attempts (1-based)."""
        return self.retry_delays[min(max(attempts, 1), len(self.retry_delays)) - 1]


@dataclass(frozen=True)
class InboxResult:
    row_id: int
    glpi_ticket_id: int
    outcome: str  # done, ignored, conflict, retry or failed
    detail: str = ""
    unavailable: bool = False  # GLPI did not answer: the inbox loop pauses


@dataclass
class ReconcileResult:
    ok: bool
    skipped: bool = False
    changed: int = 0
    queued: int = 0
    searched_after: datetime | None = None
    watermark: datetime | None = None
    error: str | None = None

    def summary(self) -> str:
        if self.skipped:
            return f"[SKIPPED] reconcile: {self.error}"
        if not self.ok:
            return f"[FAIL] reconcile: {self.error}"
        after = self.searched_after.isoformat() if self.searched_after else "-"
        mark = self.watermark.isoformat() if self.watermark else "-"
        return (
            f"[OK] reconcile: {self.changed} ticket(s) changed after {after}, "
            f"{self.queued} queued, {self.changed - self.queued} already pending; "
            f"watermark {mark}"
        )


@dataclass
class InboxDrainStats:
    outcomes: Counter = field(default_factory=Counter)
    still_pending: int = 0

    def summary(self) -> str:
        parts = ", ".join(f"{name} {count}" for name, count in sorted(self.outcomes.items()))
        return f"inbox rows: {parts or 'none'}; still pending {self.still_pending}"


class ItsmWorker:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        client: Any,
        clock: Clock,
        config: ItsmWorkerConfig | None = None,
        *,
        sync: SyncFunction = sync_ticket,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.client = client
        self.clock = clock
        self.config = config or ItsmWorkerConfig()
        self._sync = sync
        self._sleep = sleep
        self._monotonic = monotonic

    # --- housekeeping ---------------------------------------------------------------

    async def startup(self) -> int:
        """Inbox rows left 'processing' by a stopped worker go back to the queue."""
        async with session_scope(self.factory) as session:
            reset = await inbox.reset_processing(session, self.clock.now())
        if reset:
            logger.warning("itsm inbox rows recovered", extra={"rows": reset})
        return reset

    async def purge(self) -> int:
        """Delete done / ignored inbox rows older than the retention period."""
        before = self.clock.now() - self.config.retention
        async with session_scope(self.factory) as session:
            ids = (
                await session.scalars(
                    delete(ItsmInbox)
                    .where(
                        ItsmInbox.status.in_((InboxStatus.DONE, InboxStatus.IGNORED)),
                        ItsmInbox.processed_at < before,
                    )
                    .returning(ItsmInbox.id)
                    .execution_options(synchronize_session=False)
                )
            ).all()
        if ids:
            logger.info("old itsm inbox rows deleted", extra={"rows": len(ids)})
        return len(ids)

    async def pending_count(self) -> int:
        async with self.factory() as session:
            count = await session.scalar(
                select(func.count())
                .select_from(ItsmInbox)
                .where(ItsmInbox.status == InboxStatus.PENDING)
            )
        return count or 0

    # --- one inbox row ----------------------------------------------------------------

    async def process_next(self) -> InboxResult | None:
        """Sync the ticket of the next due inbox row. None when nothing is due."""
        async with session_scope(self.factory) as session:
            row = await inbox.claim_next(session, self.clock.now())
        if row is None:
            return None
        try:
            result = await self._sync(
                self.factory,
                self.client,
                row.glpi_ticket_id,
                now=self.clock.now(),
                api_username=self.config.api_username,
                quiet=row.quiet,
            )
        except ItsmUnavailable as exc:
            return await self._retry_or_fail(row, f"ItsmUnavailable: {exc}", unavailable=True)
        except ItsmRejected as exc:
            return await self._fail(row, f"ItsmRejected: {exc}")
        except Exception as exc:  # unexpected: retried, then failed
            logger.exception(
                "unexpected error while syncing a GLPI ticket",
                extra={"glpiTicketId": row.glpi_ticket_id},
            )
            return await self._retry_or_fail(row, f"{exc.__class__.__name__}: {exc}")
        note = "; ".join(result.warnings) if result.outcome == Outcome.CONFLICT else None
        async with session_scope(self.factory) as session:
            await inbox.finish(session, row.id, result.outcome, self.clock.now(), note)
        logger.info(
            "glpi ticket synced",
            extra={"glpiTicketId": row.glpi_ticket_id, "outcome": result.outcome},
        )
        return InboxResult(row.id, row.glpi_ticket_id, result.outcome, result.summary())

    async def _retry_or_fail(
        self, row: inbox.InboxRow, error: str, *, unavailable: bool = False
    ) -> InboxResult:
        if row.attempts >= self.config.max_attempts:
            return await self._fail(row, error)
        now = self.clock.now()
        available_at = now + self.config.retry_delay(row.attempts)
        async with session_scope(self.factory) as session:
            status = await inbox.release(session, row.id, error, available_at, now)
        logger.warning(
            "glpi ticket sync will be retried",
            extra={"glpiTicketId": row.glpi_ticket_id, "attempt": row.attempts, "error": error},
        )
        detail = error if status == InboxStatus.PENDING else f"{error} (a newer row will sync it)"
        return InboxResult(row.id, row.glpi_ticket_id, RETRY, detail, unavailable)

    async def _fail(self, row: inbox.InboxRow, error: str) -> InboxResult:
        async with session_scope(self.factory) as session:
            await inbox.fail(session, row.id, error, self.clock.now())
        logger.error(
            "glpi ticket sync failed", extra={"glpiTicketId": row.glpi_ticket_id, "error": error}
        )
        return InboxResult(row.id, row.glpi_ticket_id, FAILED, error)

    # --- reconciliation ---------------------------------------------------------------

    async def _claim_job(self, now: datetime, force: bool) -> tuple[bool, datetime | None] | None:
        """(claimed, watermark), or None when the job is not due (or another worker has it).
        Claiming sets next_run_at = now + interval first, so a crash cannot loop it."""
        interval = int(self.config.reconcile_interval.total_seconds())
        async with session_scope(self.factory) as session:
            await session.execute(
                ENSURE_JOB_SQL, {"job": RECONCILE_JOB, "interval": interval, "now": now}
            )
            query = select(ItsmSyncState).where(ItsmSyncState.job_name == RECONCILE_JOB)
            if not force:
                query = query.where(ItsmSyncState.next_run_at <= now)
            state = await session.scalar(
                query.with_for_update(skip_locked=True).execution_options(populate_existing=True)
            )
            if state is None:
                return None
            state.next_run_at = now + self.config.reconcile_interval
            state.interval_seconds = interval
            state.last_started_at = now
            return True, state.watermark

    async def _record(self, now: datetime, **values: Any) -> datetime | None:
        async with session_scope(self.factory) as session:
            state = await session.get(
                ItsmSyncState, RECONCILE_JOB, with_for_update=True, populate_existing=True
            )
            if values.pop("failed", False):
                state.consecutive_failures = state.consecutive_failures + 1
            newest = values.pop("newest", None)
            if newest is not None and (state.watermark is None or newest > state.watermark):
                state.watermark = newest
            for name, value in values.items():
                setattr(state, name, value)
            return state.watermark

    async def reconcile(self, *, force: bool = False) -> ReconcileResult | None:
        """Run itsm_reconcile when it is due (force: now). None when not due."""
        now = self.clock.now()
        claimed = await self._claim_job(now, force)
        if claimed is None:
            return None
        _, watermark = claimed
        if watermark is None:
            reason = "no watermark yet: run scripts.itsm_link first"
            await self._record(now, last_error=reason)
            logger.warning("itsm reconcile skipped", extra={"reason": reason})
            return ReconcileResult(ok=False, skipped=True, error=reason)
        after = watermark - self.config.reconcile_overlap
        try:
            changed = await self.client.changed_since(after)
        except Exception as exc:
            error = f"{exc.__class__.__name__}: {exc}"[:MAX_ERROR]
            await self._record(now, failed=True, last_error=error)
            logger.warning("itsm reconcile failed", extra={"error": error})
            return ReconcileResult(ok=False, searched_after=after, watermark=watermark, error=error)
        queued = 0
        async with session_scope(self.factory) as session:
            for ticket in changed:
                events = json.dumps(
                    [
                        {
                            "event": "reconcile",
                            "date_mod": ticket.date_mod.isoformat(),
                            "received_at": now.isoformat(),
                        }
                    ]
                )
                inserted = await session.scalar(
                    QUEUE_SQL,
                    {"ticket": ticket.id, "source": str(InboxSource.RECONCILE), "events": events},
                )
                queued += inserted is not None
        newest = max((ticket.date_mod for ticket in changed), default=None)
        details = {
            "lastRun": now.isoformat(),
            "searchedAfter": after.isoformat(),
            "changed": len(changed),
            "queued": queued,
            "alreadyPending": len(changed) - queued,
        }
        mark = await self._record(
            now,
            newest=newest,
            last_success_at=now,
            consecutive_failures=0,
            last_error=None,
            details=details,
        )
        logger.info("itsm reconcile finished", extra=details)
        return ReconcileResult(
            ok=True, changed=len(changed), queued=queued, searched_after=after, watermark=mark
        )

    # --- loops ------------------------------------------------------------------------

    async def drain(self) -> InboxDrainStats:
        """Process every due inbox row, then stop (rows waiting for a retry stay pending)."""
        stats = InboxDrainStats()
        await self.startup()
        while (result := await self.process_next()) is not None:
            stats.outcomes[result.outcome] += 1
            if result.unavailable:
                break  # GLPI is not answering: the rest waits for the next run
        stats.still_pending = await self.pending_count()
        return stats

    async def _wait(self, stop: asyncio.Event, seconds: float) -> None:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run_inbox_forever(self, stop: asyncio.Event) -> None:
        await self.startup()
        while not stop.is_set():
            try:
                result = await self.process_next()
            except Exception:
                logger.exception("itsm inbox loop error")
                result = None
            if result is None:
                await self._wait(stop, self.config.poll_seconds)
            elif result.unavailable:
                await self._wait(stop, self.config.retry_delay(1).total_seconds())

    async def run_scheduler_forever(self, stop: asyncio.Event) -> None:
        next_purge = 0.0
        while not stop.is_set():
            try:
                await self.reconcile()
                if self._monotonic() >= next_purge:
                    await self.purge()
                    next_purge = self._monotonic() + PURGE_EVERY_SECONDS
            except Exception:
                logger.exception("itsm scheduler loop error")
            await self._wait(stop, self.config.scheduler_poll_seconds)
