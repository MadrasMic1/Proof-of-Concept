# tests/test_itsm_sync.py
"""Phase 16: one GLPI ticket snapshot -> RIC incident (test database, no GLPI)."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.itsm import inbox
from app.itsm.errors import ItsmNotFound, ItsmUnavailable
from app.itsm.snapshot import (
    FollowupSnapshot,
    LinkedItem,
    SolutionSnapshot,
    TeamMember,
    TicketSnapshot,
)
from app.itsm.sync import Outcome, apply_snapshot, sync_ticket
from app.models import (
    AssignmentGroup,
    AuditLog,
    Incident,
    IncidentJournal,
    ItsmInbox,
    NetworkElement,
    NetworkElementType,
    Store,
)
from app.models.values import (
    ActorType,
    ContactType,
    Criticality,
    IncidentCategory,
    JournalElement,
    JournalSource,
)

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
OPENED = NOW - timedelta(hours=3)
STORE = uuid.uuid4()
GROUP_A, GROUP_B = uuid.uuid4(), uuid.uuid4()
SWITCH = uuid.uuid4()
INCIDENT = uuid.uuid4()
NUMBER = "INC09991001"
LOCATION, OTHER_LOCATION = 990001, 990009
TICKET = 990101


def snapshot(**changes: object) -> TicketSnapshot:
    base = TicketSnapshot(
        id=TICKET,
        name="ISP - Primary Link",
        content_html="<p>WAN down.</p><p>RIC details<br>Symptom: Link down</p>",
        status=2,
        type=1,
        urgency=3,
        impact=4,
        is_deleted=False,
        external_id=NUMBER,
        location_id=LOCATION,
        location_name="98711 - Sync Store",
        request_type_name="Monitoring",
        category_name="WAN",
        category_parent_name="Network",
        writer_name="ric.api",
        opened_at=OPENED,
        date_mod=NOW - timedelta(minutes=10),
        date_solve=None,
        date_close=None,
        time_to_resolve=NOW + timedelta(hours=4),
        pending_reason_name=None,
        team=(
            TeamMember("group", "assigned", 990011, "Network Ops Sync"),
            TeamMember("user", "assigned", 8, "r.kumar"),
        ),
        followups=(),
        solutions=(),
        linked_items=(LinkedItem("NetworkEquipment", 990021),),
    )
    return replace(base, **changes)


def followup(fid: int, text: str, private: bool, author: str, minutes: int) -> FollowupSnapshot:
    at = OPENED + timedelta(minutes=minutes)
    return FollowupSnapshot(fid, f"<p>{text}</p>", private, 8, author, at, at)


async def seed(session: AsyncSession, *, linked: bool = True, state: int = 2) -> None:
    session.add_all(
        [
            Store(
                id=STORE,
                site_code="98711",
                name="Sync Store",
                region="T",
                country_code="IN",
                timezone="Asia/Kolkata",
                glpi_location_id=LOCATION,
            ),
            AssignmentGroup(id=GROUP_A, name="Network Ops Sync", glpi_group_id=990011),
            AssignmentGroup(id=GROUP_B, name="Retail EUC Sync", glpi_group_id=990012),
        ]
    )
    await session.flush()
    if await session.get(NetworkElementType, "switch") is None:
        session.add(
            NetworkElementType(
                code="switch", display_name="Switch", business_criticality=Criticality.HIGH
            )
        )
        await session.flush()
    session.add(
        NetworkElement(
            id=SWITCH,
            store_id=STORE,
            element_type_code="switch",
            name="SW98711",
            serial_number="SN-98711",
            glpi_itemtype="NetworkEquipment",
            glpi_item_id=990021,
        )
    )
    await session.flush()
    session.add(
        Incident(
            id=INCIDENT,
            number=NUMBER,
            store_id=STORE,
            short_description="old title",
            category=IncidentCategory.NETWORK,
            contact_type=ContactType.PHONE,
            impact=2,
            urgency=2,
            state=state,
            hold_reason="awaiting_vendor" if state == 3 else None,
            resolved_at=NOW - timedelta(hours=1) if state in (6, 7) else None,
            close_code="Solved" if state in (6, 7) else None,
            opened_at=OPENED - timedelta(hours=1),
            glpi_ticket_id=TICKET if linked else None,
        )
    )
    await session.flush()
    session.add(
        IncidentJournal(
            incident_id=INCIDENT,
            element=JournalElement.COMMENTS,
            value="Link is down.",
            author_type=ActorType.IT_STAFF,
            created_by="r.kumar",
            source=JournalSource.SEED,
            process_status="done",
            processed_at=NOW,
            glpi_followup_id=990201 if linked else None,
            created_at=OPENED,
        )
    )
    await session.flush()


async def journal(session: AsyncSession, incident_id: uuid.UUID) -> list[IncidentJournal]:
    rows = await session.scalars(
        select(IncidentJournal)
        .where(IncidentJournal.incident_id == incident_id)
        .order_by(IncidentJournal.created_at, IncidentJournal.id)
        .execution_options(populate_existing=True)
    )
    return list(rows.all())


async def reload(session: AsyncSession, incident_id: uuid.UUID = INCIDENT) -> Incident:
    return await session.get(Incident, incident_id, populate_existing=True)


async def apply(session: AsyncSession, snap: TicketSnapshot, quiet: bool = False):
    return await apply_snapshot(session, snap, now=NOW, api_username="ric.api", quiet=quiet)


# --- existing (linked) incidents ------------------------------------------------------


async def test_update_fields_state_and_new_followups(db_session: AsyncSession) -> None:
    await seed(db_session)
    snap = snapshot(
        status=4,
        pending_reason_name="Awaiting site visit",
        followups=(
            followup(990201, "Link is down.", False, "r.kumar", 5),
            followup(990202, "Engineer onsite at 1 PM.", True, "r.kumar", 10),
            followup(990203, "[Store 98711 - Store Manager] Cabinet open", False, "ric.api", 20),
            followup(990204, "Note written by RIC", True, "ric.api", 30),
        ),
    )
    result = await apply(db_session, snap)
    assert result.outcome == Outcome.DONE and not result.created
    assert result.journal_added == 3 and result.incident_number == NUMBER
    incident = await reload(db_session)
    assert incident.short_description == "ISP - Primary Link"
    assert incident.description == "WAN down." and incident.u_symptom == "Link down"
    assert (incident.category, incident.subcategory) == ("network", "WAN")
    assert incident.contact_type == "monitoring"
    assert (incident.impact, incident.urgency) == (1, 2)
    assert (incident.state, incident.hold_reason) == (3, "awaiting_site_visit")
    assert (incident.assignment_group_id, incident.assigned_to) == (GROUP_A, "r.kumar")
    assert incident.cmdb_ci_network_element_id == SWITCH
    assert incident.opened_at == OPENED
    assert incident.u_resolution_eta == NOW + timedelta(hours=4)
    assert incident.itsm_updated_at == NOW - timedelta(minutes=10)
    assert incident.last_synced_at == NOW
    entries = {e.glpi_followup_id: e for e in await journal(db_session, INCIDENT)}
    note = entries[990202]
    assert (note.element, note.author_type, note.created_by) == (
        "work_notes",
        "it_staff",
        "r.kumar",
    )
    assert (note.source, note.process_status) == ("glpi", "pending")
    assert note.created_at == OPENED + timedelta(minutes=10)
    store = entries[990203]
    assert (store.author_type, store.created_by, store.value) == (
        "store_user",
        "Store Manager",
        "Cabinet open",
    )
    assert store.process_status == "skipped"
    assert entries[990204].process_status == "skipped"  # RIC's own write
    sources = set(
        (
            await db_session.scalars(
                select(AuditLog.source).where(AuditLog.entity_id == str(INCIDENT))
            )
        ).all()
    )
    assert sources == {"itsm_sync"}


async def test_second_sync_changes_nothing(db_session: AsyncSession) -> None:
    await seed(db_session)
    snap = snapshot(followups=(followup(990202, "Engineer onsite.", True, "r.kumar", 10),))
    await apply(db_session, snap)
    again = await apply(db_session, snap)
    assert again.outcome == Outcome.DONE
    assert again.changes == [] and again.journal_added == 0 and again.journal_updated == 0
    assert len(await journal(db_session, INCIDENT)) == 2


async def test_stale_snapshot_keeps_fields_but_adds_followups(db_session: AsyncSession) -> None:
    await seed(db_session)
    await apply(db_session, snapshot())
    old = snapshot(
        name="older title",
        date_mod=NOW - timedelta(hours=1),
        followups=(followup(990205, "Late note", True, "r.kumar", 15),),
    )
    result = await apply(db_session, old)
    assert result.stale and result.journal_added == 1
    incident = await reload(db_session)
    assert incident.short_description == "ISP - Primary Link"
    assert incident.itsm_updated_at == NOW - timedelta(minutes=10)


async def test_edited_followup_updates_text_only(db_session: AsyncSession) -> None:
    await seed(db_session)
    result = await apply(
        db_session,
        snapshot(followups=(followup(990201, "Link is DOWN (edited)", False, "r.kumar", 5),)),
    )
    assert result.journal_updated == 1 and result.journal_added == 0
    entry = (await journal(db_session, INCIDENT))[0]
    assert (entry.value, entry.process_status) == ("Link is DOWN (edited)", "done")


async def test_reassign_and_unlinked_references_are_kept(db_session: AsyncSession) -> None:
    await seed(db_session)
    await apply(db_session, snapshot())
    result = await apply(
        db_session,
        snapshot(
            team=(TeamMember("group", "assigned", 990099, "Unknown"),),
            linked_items=(LinkedItem("Computer", 990099),),
            date_mod=NOW,
        ),
    )
    incident = await reload(db_session)
    assert incident.assignment_group_id == GROUP_A  # unlinked group: kept
    assert incident.assigned_to is None  # no technician in GLPI any more
    assert incident.cmdb_ci_network_element_id == SWITCH  # unlinked asset: kept
    assert len(result.warnings) == 2
    await apply(
        db_session,
        snapshot(team=(TeamMember("group", "assigned", 990012, "Retail EUC Sync"),), date_mod=NOW),
    )
    assert (await reload(db_session)).assignment_group_id == GROUP_B


async def test_resolve_then_close(db_session: AsyncSession) -> None:
    await seed(db_session)
    solved = NOW - timedelta(minutes=30)
    solution = SolutionSnapshot(
        990301, "<p>Router replaced.</p>", "Solved (Permanently)", "s.iyer", solved
    )
    result = await apply(
        db_session,
        snapshot(
            status=6,
            solutions=(solution,),
            date_solve=solved,
            date_close=NOW - timedelta(minutes=5),
        ),
    )
    assert (
        result.outcome == Outcome.DONE
        and "resolved" in result.changes
        and "closed" in result.changes
    )
    incident = await reload(db_session)
    assert incident.state == 7
    assert incident.resolved_at == solved and incident.closed_at == NOW - timedelta(minutes=5)
    assert (incident.close_code, incident.close_notes) == (
        "Solved (Permanently)",
        "Router replaced.",
    )
    note = [e for e in await journal(db_session, INCIDENT) if e.value == "Router replaced."][0]
    assert (note.element, note.author_type, note.created_by) == ("work_notes", "it_staff", "s.iyer")
    assert note.process_status == "pending"  # the worker skips it itself: incident is closed


async def test_cancelled_solution(db_session: AsyncSession) -> None:
    await seed(db_session)
    solution = SolutionSnapshot(990302, "<p>Raised in error.</p>", "Cancelled", "s.iyer", NOW)
    await apply(db_session, snapshot(status=6, solutions=(solution,), date_close=NOW))
    incident = await reload(db_session)
    assert (incident.state, incident.close_notes) == (8, "Raised in error.")


async def test_reopen_in_glpi_is_a_conflict(db_session: AsyncSession) -> None:
    await seed(db_session, state=6)
    result = await apply(
        db_session,
        snapshot(status=2, followups=(followup(990206, "Reopened", True, "r.kumar", 50),)),
    )
    assert result.outcome == Outcome.CONFLICT
    incident = await reload(db_session)
    assert incident.state == 6
    assert incident.short_description == "ISP - Primary Link"  # fields still follow GLPI
    assert result.journal_added == 1


# --- new tickets ----------------------------------------------------------------------


async def test_new_ticket_creates_an_incident(db_session: AsyncSession) -> None:
    await seed(db_session)
    snap = snapshot(
        id=990102,
        external_id=None,
        status=4,
        pending_reason_name="Awaiting vendor",
        followups=(followup(990207, "Vendor called.", True, "r.kumar", 15),),
    )
    result = await apply(db_session, snap)
    assert result.created and result.outcome == Outcome.DONE
    assert result.incident_number and result.incident_number.startswith("INC")
    incident = await db_session.scalar(select(Incident).where(Incident.glpi_ticket_id == 990102))
    assert incident.number == result.incident_number
    assert (incident.state, incident.hold_reason) == (3, "awaiting_vendor")
    assert incident.opened_at == OPENED and incident.opened_by == "ric.api"
    assert incident.store_id == STORE and incident.cmdb_ci_network_element_id == SWITCH
    entries = await journal(db_session, incident.id)
    assert [(e.element, e.process_status) for e in entries] == [
        ("system", "pending"),  # "Incident created." -> impact assessment alert
        ("work_notes", "pending"),
    ]


async def test_new_ticket_keeps_its_unused_number_and_quiet_skips_history(
    db_session: AsyncSession,
) -> None:
    await seed(db_session)
    snap = snapshot(
        id=990103,
        external_id="INC09991002",
        followups=(followup(990208, "Old note", True, "r.kumar", 15),),
    )
    result = await apply(db_session, snap, quiet=True)
    assert result.incident_number == "INC09991002"
    incident = await db_session.scalar(select(Incident).where(Incident.number == "INC09991002"))
    statuses = {e.process_status for e in await journal(db_session, incident.id)}
    assert statuses == {"skipped"}
    assert result.skipped_entries == 1  # "Incident created."; the follow-up was added skipped


async def test_unlinked_incident_with_the_same_number_is_a_conflict(
    db_session: AsyncSession,
) -> None:
    await seed(db_session, linked=False)
    result = await apply(db_session, snapshot())
    assert result.outcome == Outcome.CONFLICT and "itsm_link" in result.warnings[-1]
    count = await db_session.scalar(
        select(func.count()).select_from(Incident).where(Incident.store_id == STORE)
    )
    assert count == 1


@pytest.mark.parametrize(
    "changes",
    [{"location_id": OTHER_LOCATION}, {"type": 2}, {"is_deleted": True}],
    ids=["not-a-ric-store", "not-an-incident", "deleted"],
)
async def test_ignored_tickets(db_session: AsyncSession, changes: dict) -> None:
    await seed(db_session)
    result = await apply(db_session, snapshot(id=990104, external_id=None, **changes))
    assert result.outcome == Outcome.IGNORED
    assert (
        await db_session.scalar(select(Incident.id).where(Incident.glpi_ticket_id == 990104))
        is None
    )


# --- sync_ticket (snapshot read + transaction + write-back) ----------------------------


class FakeClient:
    def __init__(self, snap: TicketSnapshot | Exception) -> None:
        self.snap = snap
        self.written: list[tuple[int, str]] = []

    async def get_ticket_snapshot(self, ticket_id: int) -> TicketSnapshot:
        if isinstance(self.snap, Exception):
            raise self.snap
        return self.snap

    async def set_external_id(self, ticket_id: int, number: str) -> None:
        self.written.append((ticket_id, number))


def factory_for(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=session.bind, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )


async def test_sync_ticket_writes_the_number_back(db_session: AsyncSession) -> None:
    await seed(db_session)
    client = FakeClient(snapshot(id=990105, external_id=None))
    result = await sync_ticket(
        factory_for(db_session), client, 990105, now=NOW, api_username="ric.api"
    )
    assert result.created and result.number_written_back
    assert client.written == [(990105, result.incident_number)]
    linked = FakeClient(snapshot())
    result = await sync_ticket(
        factory_for(db_session), linked, TICKET, now=NOW, api_username="ric.api"
    )
    assert linked.written == [] and not result.number_written_back


async def test_sync_ticket_errors(db_session: AsyncSession) -> None:
    gone = await sync_ticket(
        factory_for(db_session),
        FakeClient(ItsmNotFound("gone", status=404)),
        1,
        now=NOW,
        api_username="x",
    )
    assert gone.outcome == Outcome.IGNORED
    with pytest.raises(ItsmUnavailable):
        await sync_ticket(
            factory_for(db_session),
            FakeClient(ItsmUnavailable("down")),
            1,
            now=NOW,
            api_username="x",
        )


# --- inbox ------------------------------------------------------------------------------


async def test_inbox_claim_finish_release(db_session: AsyncSession) -> None:
    db_session.add_all(
        [
            ItsmInbox(
                glpi_ticket_id=990401, source="link", available_at=NOW - timedelta(minutes=2)
            ),
            ItsmInbox(
                glpi_ticket_id=990402, source="webhook", available_at=NOW - timedelta(minutes=1)
            ),
            ItsmInbox(
                glpi_ticket_id=990403, source="webhook", available_at=NOW + timedelta(hours=1)
            ),
        ]
    )
    await db_session.flush()
    first = await inbox.claim_next(db_session, NOW)
    assert (first.glpi_ticket_id, first.attempts, first.quiet) == (990401, 1, True)
    await inbox.finish(db_session, first.id, Outcome.DONE, NOW)
    second = await inbox.claim_next(db_session, NOW)
    assert (second.glpi_ticket_id, second.quiet) == (990402, False)
    assert (
        await inbox.release(db_session, second.id, "down", NOW + timedelta(minutes=1), NOW)
        == "pending"
    )
    # a newer delivery for the same ticket arrived while it was processing
    third = await inbox.claim_next(db_session, NOW + timedelta(minutes=1))
    assert third.glpi_ticket_id == 990402 and third.attempts == 2
    db_session.add(ItsmInbox(glpi_ticket_id=990402, source="webhook"))
    await db_session.flush()
    assert await inbox.release(db_session, third.id, "down", NOW, NOW) == "done"
    assert await inbox.claim_next(db_session, NOW - timedelta(hours=1)) is None
    with pytest.raises(ValueError):
        await inbox.finish(db_session, third.id, "pending", NOW)
