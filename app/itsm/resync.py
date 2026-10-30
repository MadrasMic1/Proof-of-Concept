# app/itsm/resync.py
"""Recovery helpers for the GLPI sync (Stage 2, Phase 20). Used by scripts.itsm_resync.

Everything here only QUEUES work in itsm_inbox (or sets the reconciliation start);
the worker does the syncing. The sync always reads a fresh snapshot of the ticket,
so queueing a ticket that is already in step is harmless. Every function works in
the caller's transaction (never commits).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.itsm.worker import RECONCILE_JOB
from app.models import Incident, ItsmInbox, ItsmSyncState
from app.models.values import InboxSource, InboxStatus

MAX_ERROR = 2000

QUEUE_SQL = text(
    """
    INSERT INTO itsm_inbox (glpi_ticket_id, source, events)
    VALUES (:ticket, :source, CAST(:events AS jsonb))
    ON CONFLICT (glpi_ticket_id) WHERE status = 'pending' DO NOTHING
    RETURNING id
    """
)


@dataclass(frozen=True)
class ConflictRow:
    inbox_id: int
    glpi_ticket_id: int
    incident_number: str | None
    processed_at: datetime | None
    reason: str | None


async def linked_ticket_ids(session: AsyncSession) -> list[int]:
    """GLPI ticket ids of every RIC incident linked to GLPI."""
    rows = await session.scalars(
        select(Incident.glpi_ticket_id)
        .where(Incident.glpi_ticket_id.is_not(None))
        .order_by(Incident.glpi_ticket_id)
    )
    return [int(ticket) for ticket in rows.all()]


async def ticket_of(session: AsyncSession, number: str) -> int | None:
    """The GLPI ticket id of a RIC incident number (None: not linked or unknown)."""
    return await session.scalar(select(Incident.glpi_ticket_id).where(Incident.number == number))


async def queue_tickets(
    session: AsyncSession, ticket_ids: list[int], *, now: datetime, quiet: bool = False
) -> tuple[int, int]:
    """Queue tickets for a sync. quiet: imported follow-ups are skipped (source 'link'),
    otherwise source 'manual'. A ticket with a pending row is not queued twice.
    Returns (queued, already pending)."""
    source = InboxSource.LINK if quiet else InboxSource.MANUAL
    events = json.dumps([{"event": "resync", "quiet": quiet, "at": now.isoformat()}])
    unique = list(dict.fromkeys(ticket_ids))
    queued = 0
    for ticket_id in unique:
        inserted = await session.scalar(
            QUEUE_SQL, {"ticket": ticket_id, "source": str(source), "events": events}
        )
        queued += inserted is not None
    return queued, len(unique) - queued


async def retry_failed(session: AsyncSession, *, now: datetime) -> tuple[int, int]:
    """Failed inbox rows back to pending (attempts 0, due now). A ticket that already
    has a pending row keeps that one; its failed row is closed as done ('superseded').
    Returns (requeued, superseded)."""
    rows = (
        await session.execute(
            select(ItsmInbox.id, ItsmInbox.glpi_ticket_id, ItsmInbox.last_error)
            .where(ItsmInbox.status == InboxStatus.FAILED)
            .order_by(ItsmInbox.id)
        )
    ).all()
    requeued = superseded = 0
    for row_id, ticket_id, error in rows:
        pending = await session.scalar(
            select(ItsmInbox.id).where(
                ItsmInbox.glpi_ticket_id == ticket_id, ItsmInbox.status == InboxStatus.PENDING
            )
        )
        if pending is None:
            values = {
                "status": InboxStatus.PENDING,
                "attempts": 0,
                "available_at": now,
                "processed_at": None,
                "last_error": f"retried by hand; was: {error}"[:MAX_ERROR],
            }
            requeued += 1
        else:
            values = {
                "status": InboxStatus.DONE,
                "processed_at": now,
                "last_error": f"superseded by pending row {pending}; was: {error}"[:MAX_ERROR],
            }
            superseded += 1
        await session.execute(
            update(ItsmInbox)
            .where(ItsmInbox.id == row_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
    return requeued, superseded


async def conflict_rows(
    session: AsyncSession, *, now: datetime, days: int = 7
) -> list[ConflictRow]:
    rows = await session.execute(
        select(
            ItsmInbox.id,
            ItsmInbox.glpi_ticket_id,
            Incident.number,
            ItsmInbox.processed_at,
            ItsmInbox.last_error,
        )
        .outerjoin(Incident, Incident.glpi_ticket_id == ItsmInbox.glpi_ticket_id)
        .where(
            ItsmInbox.status == InboxStatus.CONFLICT,
            ItsmInbox.processed_at >= now - timedelta(days=days),
        )
        .order_by(ItsmInbox.id.desc())
    )
    return [ConflictRow(*row) for row in rows.all()]


async def start_watermark(
    session: AsyncSession,
    glpi_time: datetime,
    *,
    interval_seconds: int,
    now: datetime,
    force: bool = False,
) -> tuple[datetime | None, datetime | None]:
    """Give the reconciliation a starting point: GLPI's current time. Kept when a
    watermark already exists (force: replace it). Returns (old, new)."""
    state = await session.get(
        ItsmSyncState, RECONCILE_JOB, with_for_update=True, populate_existing=True
    )
    if state is None:
        state = ItsmSyncState(
            job_name=RECONCILE_JOB, interval_seconds=interval_seconds, next_run_at=now
        )
        session.add(state)
    old = state.watermark
    if old is None or force:
        state.watermark = glpi_time
    await session.flush()
    return old, state.watermark
