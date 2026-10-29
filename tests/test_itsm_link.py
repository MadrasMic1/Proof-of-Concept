# tests/test_itsm_link.py
"""Phase 15: linking RIC rows to existing GLPI objects (no GLPI needed)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.itsm.glpi_client import GlpiClient, GlpiConfig
from app.itsm.link import (
    AssetRow,
    GlpiInventory,
    GroupRow,
    IncidentRow,
    JournalRow,
    RicState,
    StoreRow,
    apply_plan,
    load_ric_state,
    plan_links,
)
from app.itsm.mapping import JournalFields
from app.itsm.snapshot import GlpiAsset, GlpiNamed, GlpiTicketRef
from app.models import (
    AssignmentGroup,
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
    ContactType,
    Criticality,
    IncidentCategory,
    JournalElement,
    JournalSource,
)

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
STORE_A, STORE_B = uuid.uuid4(), uuid.uuid4()
GROUP = uuid.uuid4()
SWITCH_A, SWITCH_B, POS_A = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
INC_A = uuid.uuid4()


def glpi(**changes: object) -> GlpiInventory:
    values: dict[str, object] = {
        "locations": [
            GlpiNamed(900001, "98701 - Test Store A"),
            GlpiNamed(900002, "98702 - Test Store B"),
            GlpiNamed(900003, "Head office"),
        ],
        "groups": [GlpiNamed(900011, "Network Ops"), GlpiNamed(900012, "Service Desk")],
        "assets": [
            GlpiAsset("NetworkEquipment", 900021, "SW-1", 900001),
            GlpiAsset("NetworkEquipment", 900022, "SW-1", 900002),
            GlpiAsset("Computer", 900023, "POS Terminal 03", 900001),
        ],
        "tickets": [
            GlpiTicketRef(900031, "INC09990001", 900001, 1, NOW - timedelta(hours=2), False),
            GlpiTicketRef(900032, None, 900002, 1, NOW - timedelta(hours=1), False),
            GlpiTicketRef(900033, None, 900003, 1, NOW - timedelta(minutes=30), False),
            GlpiTicketRef(900034, None, 900001, 2, NOW - timedelta(minutes=20), False),
            GlpiTicketRef(900035, "INC09990002", 900001, 1, NOW, True),
        ],
    }
    values.update(changes)
    return GlpiInventory(**values)  # type: ignore[arg-type]


def ric(**changes: object) -> RicState:
    values: dict[str, object] = {
        "stores": [StoreRow(STORE_A, "98701", None), StoreRow(STORE_B, "98702", None)],
        "groups": [GroupRow(GROUP, "Network Ops", None)],
        "assets": [
            AssetRow(SWITCH_A, "network_element", STORE_A, "SW-1", None, None),
            AssetRow(SWITCH_B, "network_element", STORE_B, "SW-1", None, None),
            AssetRow(POS_A, "device", STORE_A, "POS Terminal 03", None, None),
        ],
        "incidents": [IncidentRow(INC_A, "INC09990001", STORE_A, None)],
        "journal": [],
    }
    values.update(changes)
    return RicState(**values)  # type: ignore[arg-type]


def entry(fid: int, element: str, value: str) -> JournalFields:
    return JournalFields(
        glpi_followup_id=fid,
        element=element,
        value=value,
        author_type="it_staff",
        created_by="r.kumar",
        created_at=NOW,
        updated_at=NOW,
        own_write=False,
        store_site=None,
    )


def jrow(element: str, value: str, minutes: int, fid: int | None = None) -> JournalRow:
    return JournalRow(uuid.uuid4(), INC_A, element, value, NOW + timedelta(minutes=minutes), fid)


# --- planning (pure) --------------------------------------------------------------------


def test_first_link_of_everything() -> None:
    plan = plan_links(ric(), glpi())
    assert plan.store_links == {STORE_A: 900001, STORE_B: 900002}
    assert plan.group_links == {GROUP: 900011}
    assert plan.element_links == {
        SWITCH_A: ("NetworkEquipment", 900021),
        SWITCH_B: ("NetworkEquipment", 900022),
    }
    assert plan.device_links == {POS_A: ("Computer", 900023)}
    assert plan.incident_links == {INC_A: 900031}
    assert plan.incident_tickets == {INC_A: 900031}
    # incident tickets at RIC stores only (not head office, not a request, not deleted)
    assert plan.queue == [900031, 900032]
    assert plan.glpi_only_tickets == [900032]
    assert plan.watermark == NOW - timedelta(minutes=20)
    assert plan.ric_only_incidents == []
    assert plan.problems == 0
    assert plan.reports["stores"].linked == 2 and plan.reports["assets"].linked == 3


def test_second_run_changes_nothing() -> None:
    linked = ric(
        stores=[StoreRow(STORE_A, "98701", 900001), StoreRow(STORE_B, "98702", 900002)],
        groups=[GroupRow(GROUP, "Network Ops", 900011)],
        assets=[
            AssetRow(SWITCH_A, "network_element", STORE_A, "SW-1", "NetworkEquipment", 900021),
            AssetRow(SWITCH_B, "network_element", STORE_B, "SW-1", "NetworkEquipment", 900022),
            AssetRow(POS_A, "device", STORE_A, "POS Terminal 03", "Computer", 900023),
        ],
        incidents=[IncidentRow(INC_A, "INC09990001", STORE_A, 900031)],
    )
    plan = plan_links(linked, glpi())
    assert not (plan.store_links or plan.group_links or plan.element_links)
    assert not (plan.device_links or plan.incident_links)
    assert plan.reports["stores"].already == 2 and plan.reports["assets"].already == 3
    assert plan.queue == [900031, 900032]


def test_assets_need_their_store_linked() -> None:
    plan = plan_links(ric(), glpi(locations=[GlpiNamed(900001, "98701 - Test Store A")]))
    assert SWITCH_B not in plan.element_links
    assert plan.reports["stores"].no_match == 1
    assert plan.reports["assets"].no_match == 1
    assert plan.queue == [900031]


def test_ambiguous_and_missing_matches_are_left_alone() -> None:
    plan = plan_links(
        ric(),
        glpi(
            groups=[GlpiNamed(900011, "Network Ops"), GlpiNamed(900013, "Network Ops")],
            tickets=[GlpiTicketRef(900031, "INC09990009", 900001, 1, NOW, False)],
        ),
    )
    assert plan.group_links == {}
    assert plan.reports["groups"].ambiguous == 1
    assert plan.incident_links == {}
    assert plan.ric_only_incidents == ["INC09990001"]
    assert plan.problems == 1


def test_existing_link_changes_only_with_relink() -> None:
    state = ric(stores=[StoreRow(STORE_A, "98701", 900009), StoreRow(STORE_B, "98702", None)])
    plan = plan_links(state, glpi())
    assert STORE_A not in plan.store_links
    assert plan.reports["stores"].conflict == 1
    assert "--relink" in plan.reports["stores"].details[0]
    plan = plan_links(state, glpi(), relink=True)
    assert plan.store_links[STORE_A] == 900001


def test_glpi_object_linked_to_another_row_is_a_conflict() -> None:
    state = ric(stores=[StoreRow(STORE_A, "98701", None), StoreRow(STORE_B, "98702", 900001)])
    plan = plan_links(state, glpi(), relink=True)
    assert STORE_A not in plan.store_links
    assert plan.store_links == {STORE_B: 900002}
    assert plan.reports["stores"].conflict == 1


def test_journal_pairs_same_kind_and_text_oldest_first() -> None:
    note1 = jrow(JournalElement.WORK_NOTES, "Engineer  onsite\nat 1 PM.", 1)
    note2 = jrow(JournalElement.WORK_NOTES, "Engineer onsite at 1 PM.", 2)
    comment = jrow(JournalElement.COMMENTS, "Link is down.", 3)
    system = jrow(JournalElement.SYSTEM, "Incident created.", 0)
    close = jrow(JournalElement.WORK_NOTES, "Close notes only in RIC.", 4)
    already = jrow(JournalElement.COMMENTS, "Old comment", 5, fid=900049)
    followups = {
        900031: [
            entry(900041, "work_notes", "Engineer onsite at 1 PM."),
            entry(900042, "work_notes", "Engineer onsite at 1 PM."),
            entry(900043, "work_notes", "Link is down."),
            entry(900044, "comments", "Link is down."),
            entry(900049, "comments", "Old comment"),
            entry(900045, "comments", "Only in GLPI"),
        ]
    }
    state = ric(journal=[note1, note2, comment, system, close, already])
    plan = plan_links(state, glpi(), followups)
    assert plan.journal_links == {note1.id: 900041, note2.id: 900042, comment.id: 900044}
    report = plan.reports["journal"]
    assert (report.linked, report.already, report.no_match) == (3, 1, 1)
    assert plan.glpi_only_followups == 2


# --- database ---------------------------------------------------------------------------


async def _seed(session: AsyncSession) -> None:
    session.add_all(
        [
            Store(
                id=STORE_A,
                site_code="98701",
                name="A",
                region="T",
                country_code="IN",
                timezone="Asia/Kolkata",
            ),
            Store(
                id=STORE_B,
                site_code="98702",
                name="B",
                region="T",
                country_code="IN",
                timezone="Asia/Kolkata",
            ),
            AssignmentGroup(id=GROUP, name="Network Ops 98701"),
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
    session.add_all(
        [
            NetworkElement(
                id=SWITCH_A,
                store_id=STORE_A,
                element_type_code="switch",
                name="SW-1",
                serial_number="SN-98701-1",
            ),
            NetworkElement(
                id=SWITCH_B,
                store_id=STORE_B,
                element_type_code="switch",
                name="SW-1",
                serial_number="SN-98702-1",
            ),
            Incident(
                id=INC_A,
                number="INC09990001",
                store_id=STORE_A,
                short_description="x",
                category=IncidentCategory.NETWORK,
                contact_type=ContactType.MONITORING,
                impact=1,
                urgency=2,
                opened_at=NOW,
            ),
        ]
    )
    await session.flush()
    session.add(
        IncidentJournal(
            incident_id=INC_A,
            element=JournalElement.COMMENTS,
            value="Link is down.",
            author_type=ActorType.IT_STAFF,
            created_by="r.kumar",
            source=JournalSource.SEED,
        )
    )
    await session.flush()


def _db_glpi() -> GlpiInventory:
    return glpi(
        groups=[GlpiNamed(900011, "Network Ops 98701")],
        assets=[
            GlpiAsset("NetworkEquipment", 900021, "SW-1", 900001),
            GlpiAsset("NetworkEquipment", 900022, "SW-1", 900002),
        ],
    )


async def test_apply_writes_links_queue_and_watermark(db_session: AsyncSession) -> None:
    await _seed(db_session)
    followups = {900031: [entry(900044, "comments", "Link is down.")]}
    plan = plan_links(await load_ric_state(db_session), _db_glpi(), followups)
    queued, already, watermark, next_run_at = await apply_plan(
        db_session, plan, now=NOW, interval_seconds=900
    )
    assert (queued, already) == (2, 0)
    assert watermark == NOW - timedelta(minutes=20)
    assert next_run_at == NOW

    assert (await db_session.get(Store, STORE_A)).glpi_location_id == 900001
    assert (await db_session.get(AssignmentGroup, GROUP)).glpi_group_id == 900011
    switch = await db_session.get(NetworkElement, SWITCH_B)
    assert (switch.glpi_itemtype, switch.glpi_item_id) == ("NetworkEquipment", 900022)
    assert (await db_session.get(Incident, INC_A)).glpi_ticket_id == 900031
    fid = await db_session.scalar(
        select(IncidentJournal.glpi_followup_id).where(IncidentJournal.incident_id == INC_A)
    )
    assert fid == 900044
    inbox = (
        await db_session.scalars(
            select(ItsmInbox).where(ItsmInbox.glpi_ticket_id.in_([900031, 900032]))
        )
    ).all()
    assert sorted(row.glpi_ticket_id for row in inbox) == [900031, 900032]
    assert {(row.source, row.status) for row in inbox} == {("link", "pending")}

    # second run: everything already linked, nothing queued twice, watermark kept
    again = plan_links(await load_ric_state(db_session), _db_glpi(), followups)
    assert again.reports["incidents"].already == 1
    assert again.reports["journal"].already == 1
    assert not (again.store_links or again.incident_links or again.journal_links)
    older = GlpiTicketRef(900031, "INC09990001", 900001, 1, NOW - timedelta(days=1), False)
    again.watermark = older.date_mod
    queued, already, watermark, _ = await apply_plan(
        db_session, again, now=NOW + timedelta(hours=1), interval_seconds=900
    )
    assert (queued, already) == (0, 2)
    assert watermark == NOW - timedelta(minutes=20)
    state = await db_session.get(ItsmSyncState, "itsm_reconcile")
    assert state.interval_seconds == 900


async def test_relink_can_move_a_link_to_an_id_another_row_gives_up(
    db_session: AsyncSession,
) -> None:
    await _seed(db_session)
    a = await db_session.get(Store, STORE_A)
    b = await db_session.get(Store, STORE_B)
    a.glpi_location_id, b.glpi_location_id = 900002, 900009
    await db_session.flush()
    # GLPI now says: A -> 900001 (free), B -> 900002 (held by A, freed in the same run)
    plan = plan_links(await load_ric_state(db_session), _db_glpi(), relink=True)
    await apply_plan(db_session, plan, now=NOW, interval_seconds=900)
    await db_session.refresh(a)
    assert a.glpi_location_id == 900001


# --- client lists (fake GLPI V1) --------------------------------------------------------

CONFIG = GlpiConfig(
    v2_url="http://glpi.test/api.php/v2",
    token_url="http://glpi.test/api.php/token",
    client_id="c",
    client_secret="s",
    username="ric.api",
    password="p",
    v1_url="http://glpi.test/apirest.php",
    user_token="u",
    app_token="a",
)


def fake_v1(request: httpx.Request) -> httpx.Response:
    path = request.url.path.removeprefix("/apirest.php/")
    if path == "initSession":
        return httpx.Response(200, json={"session_token": "s1"})
    start = int(request.url.params.get("range", "0-999").split("-")[0])
    if path == "Location":
        return httpx.Response(200, json=[{"id": 3, "name": "01234 - Chennai Anna Nagar"}])
    if path == "Group":
        return httpx.Response(200, json=[{"id": 1, "name": "Network Ops"}, {"id": 0, "name": "x"}])
    if path == "NetworkEquipment":
        if start == 0:
            items = [{"id": n, "name": f"SW{n}", "locations_id": 3} for n in range(1, 1001)]
            return httpx.Response(206, json=items)
        return httpx.Response(400, json=["ERROR_RANGE_EXCEED_TOTAL", "Provided range exceed"])
    if path == "Computer":
        return httpx.Response(200, json=[{"id": 7, "name": "POS Terminal 03", "locations_id": 0}])
    if path in ("Printer", "Peripheral"):
        return httpx.Response(200, json=[])
    if path == "Ticket":
        return httpx.Response(
            200,
            json=[
                {
                    "id": 3,
                    "externalid": "INC00001234",
                    "locations_id": 3,
                    "type": 1,
                    "date_mod": "2026-10-25 18:25:38",
                    "is_deleted": 0,
                },
                {
                    "id": 4,
                    "externalid": "",
                    "locations_id": 0,
                    "type": 2,
                    "date_mod": None,
                    "is_deleted": 0,
                },
            ],
        )
    return httpx.Response(404, json=["ERROR_ITEM_NOT_FOUND", ""])


async def test_client_lists() -> None:
    client = GlpiClient(CONFIG, transport=httpx.MockTransport(fake_v1))
    assert await client.list_locations() == [GlpiNamed(3, "01234 - Chennai Anna Nagar")]
    assert await client.list_groups() == [GlpiNamed(1, "Network Ops")]
    assets = await client.list_assets()
    assert len(assets) == 1001
    assert assets[-1] == GlpiAsset("Computer", 7, "POS Terminal 03", None)
    tickets = await client.list_tickets()
    assert tickets[0] == GlpiTicketRef(
        3, "INC00001234", 3, 1, datetime(2026, 10, 25, 18, 25, 38, tzinfo=UTC), False
    )
    assert tickets[1] == GlpiTicketRef(4, None, None, 2, None, False)
    await client.aclose()
