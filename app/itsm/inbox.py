# app/itsm/inbox.py
"""The sync work queue (itsm_inbox): claim, finish, release, fail.

One row = one GLPI ticket to sync. At most one PENDING row per ticket (unique
index ux_itsm_inbox_one_pending), so deliveries that arrive together merge.
claim_next takes the oldest due row with FOR UPDATE SKIP LOCKED, so two workers
never take the same row. Used by scripts.itsm_sync (Phase 16) and the worker
(Phase 18). Every function works in the caller's transaction (never commits).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ItsmInbox
from app.models.values import InboxSource, InboxStatus

MAX_ERROR = 2000
FINISHED = (InboxStatus.DONE, InboxStatus.IGNORED, InboxStatus.CONFLICT)

# Back to pending only when the ticket has no other pending row (a newer delivery);
# otherwise that newer row will sync the ticket and this one is closed as done.
RELEASE_SQL = text(
    """
    UPDATE itsm_inbox
    SET status = CASE WHEN EXISTS (
            SELECT 1 FROM itsm_inbox other
            WHERE other.glpi_ticket_id = itsm_inbox.glpi_ticket_id
              AND other.status = 'pending' AND other.id <> itsm_inbox.id)
        THEN 'done' ELSE 'pending' END,
        processed_at = CASE WHEN EXISTS (
            SELECT 1 FROM itsm_inbox other
            WHERE other.glpi_ticket_id = itsm_inbox.glpi_ticket_id
              AND other.status = 'pending' AND other.id <> itsm_inbox.id)
        THEN CAST(:now AS timestamptz) ELSE NULL END,
        available_at = :available_at,
        last_error = :error
    WHERE id = :id
    RETURNING status
    """
)


@dataclass(frozen=True)
class InboxRow:
    id: int
    glpi_ticket_id: int
    source: str
    attempts: int  # including this attempt

    @property
    def quiet(self) -> bool:
        """The first sync after itsm_link imports history: no LLM processing."""
        return self.source == InboxSource.LINK


async def claim_next(session: AsyncSession, now: datetime) -> InboxRow | None:
    """Mark the oldest due pending row 'processing' (attempts + 1). The caller commits."""
    row = await session.scalar(
        select(ItsmInbox)
        .where(ItsmInbox.status == InboxStatus.PENDING, ItsmInbox.available_at <= now)
        .order_by(ItsmInbox.available_at, ItsmInbox.id)
        .limit(1)
        .with_for_update(skip_locked=True)
        .execution_options(populate_existing=True)
    )
    if row is None:
        return None
    row.status = InboxStatus.PROCESSING
    row.attempts = row.attempts + 1
    await session.flush()
    return InboxRow(row.id, row.glpi_ticket_id, row.source, row.attempts)


async def finish(
    session: AsyncSession, row_id: int, status: str, now: datetime, note: str | None = None
) -> None:
    """done / ignored / conflict (note: why, kept in last_error for the status report)."""
    if status not in FINISHED:
        raise ValueError(f"not a finished inbox status: {status!r}")
    await session.execute(
        update(ItsmInbox)
        .where(ItsmInbox.id == row_id)
        .values(status=status, processed_at=now, last_error=note[:MAX_ERROR] if note else None)
        .execution_options(synchronize_session=False)
    )


async def release(
    session: AsyncSession, row_id: int, error: str, available_at: datetime, now: datetime
) -> str:
    """GLPI was unavailable: try again at available_at. Returns the new status."""
    status = await session.scalar(
        RELEASE_SQL,
        {"id": row_id, "error": error[:MAX_ERROR], "available_at": available_at, "now": now},
    )
    return str(status)


async def fail(session: AsyncSession, row_id: int, error: str, now: datetime) -> None:
    await session.execute(
        update(ItsmInbox)
        .where(ItsmInbox.id == row_id)
        .values(status=InboxStatus.FAILED, processed_at=now, last_error=error[:MAX_ERROR])
        .execution_options(synchronize_session=False)
    )


async def reset_processing(session: AsyncSession, now: datetime) -> int:
    """Rows left 'processing' by a stopped run go back to pending (or done if superseded)."""
    ids = (
        await session.scalars(
            select(ItsmInbox.id).where(ItsmInbox.status == InboxStatus.PROCESSING)
        )
    ).all()
    for row_id in ids:
        await release(session, row_id, "interrupted", now, now)
    return len(ids)
