# tests/test_itsm_schema.py
"""Revision 0004: GLPI ids, the sync inbox and the sync job state."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AuditLog,
    Incident,
    IncidentJournal,
    ItsmInbox,
    ItsmSyncState,
    NetworkElement,
    NetworkElementType,
    Store,
)
from app.models.values import (
    ActorType,
    AuditAction,
    AuditSource,
    ContactType,
    Criticality,
    GlpiItemtype,
    InboxSource,
    InboxStatus,
    IncidentCategory,
    JournalElement,
    JournalSource,
)

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)

# The statement the webhook route will use (Phase 17): one pending row per ticket.
MERGE_SQL = text(
    """
    INSERT INTO itsm_inbox (glpi_ticket_id, source, events)
    VALUES (:ticket, 'webhook', CAST(:events AS jsonb))
    ON CONFLICT (glpi_ticket_id) WHERE status = 'pending'
    DO UPDATE SET events = itsm_inbox.events || EXCLUDED.events,
                  merged_count = itsm_inbox.merged_count + 1
    RETURNING id, merged_count
    """
)


async def add(session: AsyncSession, obj: object) -> None:
    session.add(obj)
    await session.flush()


async def expect_rejected(session: AsyncSession, obj: object) -> None:
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(obj)
            await session.flush()


def new_store(site_code: str, glpi_location_id: int | None = None) -> Store:
    return Store(
        site_code=site_code,
        name=f"Store {site_code}",
        region="UK",
        country_code="GB",
        timezone="Europe/London",
        glpi_location_id=glpi_location_id,
    )


def new_incident(store: Store, number: str, glpi_ticket_id: int | None) -> Incident:
    return Incident(
        number=number,
        store_id=store.id,
        short_description="ISP - Primary Link",
        category=IncidentCategory.NETWORK,
        contact_type=ContactType.MONITORING,
        impact=1,
        urgency=2,
        opened_at=NOW,
        glpi_ticket_id=glpi_ticket_id,
    )


def new_journal(incident: Incident, glpi_followup_id: int) -> IncidentJournal:
    return IncidentJournal(
        incident_id=incident.id,
        element=JournalElement.WORK_NOTES,
        value="AT&T engineer onsite at 1 PM.",
        author_type=ActorType.IT_STAFF,
        created_by="r.kumar",
        source=JournalSource.GLPI,
        glpi_followup_id=glpi_followup_id,
    )


async def make_switch(session: AsyncSession, store: Store, name: str, **glpi: object) -> None:
    if await session.get(NetworkElementType, "switch") is None:
        await add(
            session,
            NetworkElementType(
                code="switch",
                display_name="Switch",
                business_criticality=Criticality.HIGH,
                aruba_device_type="SWITCH",
            ),
        )
    await add(
        session,
        NetworkElement(
            store_id=store.id,
            element_type_code="switch",
            name=name,
            serial_number=f"SN-{name}",
            **glpi,
        ),
    )


async def test_glpi_location_id_is_unique(db_session: AsyncSession) -> None:
    await add(db_session, new_store("98701", glpi_location_id=900003))
    await expect_rejected(db_session, new_store("98702", glpi_location_id=900003))
    await add(db_session, new_store("98703"))
    await add(db_session, new_store("98704"))


async def test_glpi_asset_pair(db_session: AsyncSession) -> None:
    store = new_store("98701")
    await add(db_session, store)
    await make_switch(
        db_session,
        store,
        "SW98701A",
        glpi_itemtype=GlpiItemtype.NETWORK_EQUIPMENT,
        glpi_item_id=900041,
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_switch(
                db_session,
                store,
                "SW98701B",
                glpi_itemtype=GlpiItemtype.NETWORK_EQUIPMENT,
                glpi_item_id=900041,
            )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_switch(db_session, store, "SW98701C", glpi_itemtype="Computer")
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            await make_switch(
                db_session, store, "SW98701D", glpi_itemtype="Router", glpi_item_id=900007
            )


async def test_glpi_ticket_and_followup_ids_are_unique(db_session: AsyncSession) -> None:
    store = new_store("98701")
    await add(db_session, store)
    incident = new_incident(store, "INC09990001", 900003)
    await add(db_session, incident)
    await expect_rejected(db_session, new_incident(store, "INC09990002", 900003))
    await add(db_session, new_journal(incident, 900041))
    await expect_rejected(db_session, new_journal(incident, 900041))
    assert incident.itsm_updated_at is None
    assert incident.last_synced_at is None


async def test_audit_source_itsm_sync(db_session: AsyncSession) -> None:
    await add(
        db_session,
        AuditLog(
            actor_type=ActorType.SYSTEM,
            actor="itsm-sync",
            source=AuditSource.ITSM_SYNC,
            action=AuditAction.UPDATE,
            entity_type="incident",
            entity_id="INC09990001",
        ),
    )


async def test_inbox_defaults(db_session: AsyncSession) -> None:
    row = ItsmInbox(glpi_ticket_id=900003, source=InboxSource.WEBHOOK)
    await add(db_session, row)
    assert row.status == InboxStatus.PENDING
    assert row.events == []
    assert row.merged_count == 1
    assert row.attempts == 0


async def test_inbox_one_pending_row_per_ticket(db_session: AsyncSession) -> None:
    await add(db_session, ItsmInbox(glpi_ticket_id=900003, source=InboxSource.WEBHOOK))
    await expect_rejected(
        db_session, ItsmInbox(glpi_ticket_id=900003, source=InboxSource.RECONCILE)
    )
    # A pending row next to a processing row of the same ticket is allowed.
    await add(
        db_session,
        ItsmInbox(glpi_ticket_id=900004, source=InboxSource.WEBHOOK, status=InboxStatus.PROCESSING),
    )
    await add(db_session, ItsmInbox(glpi_ticket_id=900004, source=InboxSource.WEBHOOK))


async def test_inbox_merge_statement(db_session: AsyncSession) -> None:
    first = (
        await db_session.execute(
            MERGE_SQL, {"ticket": 900007, "events": json.dumps([{"e": "new"}])}
        )
    ).one()
    second = (
        await db_session.execute(
            MERGE_SQL, {"ticket": 900007, "events": json.dumps([{"e": "upd"}])}
        )
    ).one()
    assert second.id == first.id
    assert second.merged_count == 2
    events = await db_session.scalar(select(ItsmInbox.events).where(ItsmInbox.id == first.id))
    assert events == [{"e": "new"}, {"e": "upd"}]


async def test_inbox_finished_rows_need_processed_at(db_session: AsyncSession) -> None:
    await expect_rejected(
        db_session, ItsmInbox(glpi_ticket_id=900009, source=InboxSource.WEBHOOK, status="done")
    )
    await expect_rejected(
        db_session, ItsmInbox(glpi_ticket_id=900009, source=InboxSource.WEBHOOK, status="unknown")
    )
    await add(
        db_session,
        ItsmInbox(
            glpi_ticket_id=900009, source=InboxSource.MANUAL, status="done", processed_at=NOW
        ),
    )


async def test_sync_state(db_session: AsyncSession) -> None:
    state = ItsmSyncState(job_name="itsm_test_job", interval_seconds=900, next_run_at=NOW)
    await add(db_session, state)
    assert state.details == {}
    assert state.consecutive_failures == 0
    assert state.watermark is None
    await expect_rejected(
        db_session,
        ItsmSyncState(job_name="Bad Name", interval_seconds=900, next_run_at=NOW),
    )
