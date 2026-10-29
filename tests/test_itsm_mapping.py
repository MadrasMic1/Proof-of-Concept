# tests/test_itsm_mapping.py
"""GLPI ticket snapshot -> RIC incident and journal values (pure functions)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.itsm.mapping import (
    html_to_text,
    map_incident,
    map_journal,
    map_level,
    map_state,
    split_description,
    split_store_prefix,
    store_prefix,
    text_to_html,
)
from app.itsm.snapshot import (
    FollowupSnapshot,
    LinkedItem,
    SolutionSnapshot,
    TeamMember,
    TicketSnapshot,
)

OPENED = datetime(2026, 10, 28, 8, 0, tzinfo=UTC)
MODIFIED = OPENED + timedelta(hours=2)
SEEDED_CONTENT = (
    "<p>Aruba reports WAN health DOWN on GW01234N001 uplink WAN1.</p>"
    "<p>RIC details<br>RIC incident: INC00001234<br>Symptom: Retail SDWAN ATTLink Status"
    "<br>Business service: AT&amp;T SDWAN Linkdown<br>Opened by: Aruba Central"
    "<br>ETA note: Awaiting update from ISP</p>"
)


def ticket(**changes: object) -> TicketSnapshot:
    base = TicketSnapshot(
        id=3,
        name="ISP - Primary Link",
        content_html=SEEDED_CONTENT,
        status=4,
        type=1,
        urgency=3,
        impact=4,
        is_deleted=False,
        external_id="INC00001234",
        location_id=3,
        location_name="01234 - Chennai Anna Nagar",
        request_type_name="Monitoring",
        category_name="WAN",
        category_parent_name="Network",
        writer_name="ric.api",
        opened_at=OPENED,
        date_mod=MODIFIED,
        date_solve=None,
        date_close=None,
        time_to_resolve=None,
        pending_reason_name="Awaiting site visit",
        team=(
            TeamMember(kind="group", role="assigned", id=5, name="Network Ops"),
            TeamMember(kind="user", role="assigned", id=8, name="r.kumar"),
            TeamMember(kind="user", role="requester", id=2, name="glpi"),
        ),
        linked_items=(LinkedItem(itemtype="NetworkEquipment", item_id=41),),
    )
    return replace(base, **changes)


def followup(fid: int, content: str, private: bool, author: str | None, minutes: int):
    return FollowupSnapshot(
        id=fid,
        content_html=content,
        is_private=private,
        author_id=8,
        author_name=author,
        created_at=OPENED + timedelta(minutes=minutes),
        updated_at=OPENED + timedelta(minutes=minutes),
    )


def solution(sid: int, type_name: str | None, content: str = "<p>Fixed.</p>") -> SolutionSnapshot:
    return SolutionSnapshot(
        id=sid, content_html=content, type_name=type_name, author_name="s.iyer", created_at=MODIFIED
    )


def test_seeded_ticket_maps_to_ric_values() -> None:
    fields = map_incident(ticket())
    assert fields.glpi_ticket_id == 3
    assert fields.number == "INC00001234"
    assert fields.glpi_location_id == 3
    assert fields.short_description == "ISP - Primary Link"
    assert fields.description == "Aruba reports WAN health DOWN on GW01234N001 uplink WAN1."
    assert fields.u_symptom == "Retail SDWAN ATTLink Status"
    assert fields.business_service == "AT&T SDWAN Linkdown"
    assert fields.opened_by == "Aruba Central"
    assert fields.u_eta_note == "Awaiting update from ISP"
    assert fields.caller is None
    assert (fields.category, fields.subcategory) == ("network", "WAN")
    assert fields.contact_type == "monitoring"
    assert (fields.impact, fields.urgency) == (1, 2)
    assert (fields.state, fields.hold_reason) == (3, "awaiting_site_visit")
    assert (fields.glpi_group_id, fields.group_name, fields.assigned_to) == (
        5,
        "Network Ops",
        "r.kumar",
    )
    assert fields.ci == LinkedItem(itemtype="NetworkEquipment", item_id=41)
    assert (fields.opened_at, fields.itsm_updated_at) == (OPENED, MODIFIED)
    assert fields.is_incident and not fields.is_deleted
    assert fields.warnings == ()


@pytest.mark.parametrize(("status", "expected"), [(1, 1), (2, 2), (3, 2), (4, 3), (5, 6), (6, 7)])
def test_status_to_state(status: int, expected: int) -> None:
    assert map_state(status, None) == expected


def test_cancelled_solution_is_state_8() -> None:
    assert map_state(6, solution(1, "Cancelled")) == 8
    assert map_state(5, solution(1, "Cancelled")) == 8
    assert map_state(6, solution(1, "Solved (Permanently)")) == 7
    with pytest.raises(ValueError):
        map_state(9, None)


@pytest.mark.parametrize(("glpi", "ric"), [(5, 1), (4, 1), (3, 2), (2, 3), (1, 3), (0, 2)])
def test_levels(glpi: int, ric: int) -> None:
    assert map_level(glpi) == ric


def test_pending_without_reason_defaults_with_warning() -> None:
    fields = map_incident(ticket(pending_reason_name=None))
    assert fields.hold_reason == "awaiting_caller"
    assert any("pending reason" in w for w in fields.warnings)


def test_hold_reason_only_when_on_hold() -> None:
    fields = map_incident(ticket(status=2))
    assert (fields.state, fields.hold_reason) == (2, None)


def test_resolved_ticket_takes_latest_solution() -> None:
    solved = MODIFIED + timedelta(minutes=5)
    fields = map_incident(
        ticket(
            status=5,
            date_solve=solved,
            solutions=(
                solution(1, "Other", "<p>Old</p>"),
                replace(
                    solution(2, "Solved (Permanently)", "<p>Roller replaced.</p>"),
                    created_at=solved,
                ),
            ),
        )
    )
    assert fields.state == 6
    assert (fields.close_code, fields.close_notes) == ("Solved (Permanently)", "Roller replaced.")
    assert fields.resolved_at == solved
    assert fields.closed_at is None


def test_closed_ticket_without_dates_gets_fallbacks() -> None:
    fields = map_incident(ticket(status=6, solutions=(solution(1, "Solved (Permanently)"),)))
    assert fields.state == 7
    assert fields.resolved_at == MODIFIED and fields.closed_at == MODIFIED
    assert len(fields.warnings) == 2


def test_resolved_before_opened_is_clamped() -> None:
    fields = map_incident(
        ticket(status=5, date_solve=OPENED - timedelta(hours=1), solutions=(solution(1, "x"),))
    )
    assert fields.resolved_at == OPENED


def test_unknown_values_get_defaults_and_warnings() -> None:
    fields = map_incident(
        ticket(
            category_name="Printers",
            category_parent_name=None,
            request_type_name="Chat",
            external_id="TEST-1",
            name="",
            content_html="<p>Plain text only</p>",
            opened_at=None,
            writer_name="r.kumar",
        )
    )
    assert (fields.category, fields.subcategory) == ("inquiry", "Printers")
    assert fields.contact_type == "phone"
    assert fields.number is None
    assert fields.short_description == "Plain text only"
    assert fields.description == "Plain text only"
    assert fields.opened_by == "r.kumar"
    assert fields.opened_at == MODIFIED
    assert len(fields.warnings) == 3


def test_top_level_category() -> None:
    fields = map_incident(ticket(category_name="Inquiry", category_parent_name=None))
    assert (fields.category, fields.subcategory) == ("inquiry", None)


def test_first_assigned_group_user_and_asset_with_warnings() -> None:
    fields = map_incident(
        ticket(
            team=(
                TeamMember(kind="group", role="assigned", id=5, name="Network Ops"),
                TeamMember(kind="group", role="assigned", id=6, name="Retail EUC Ops"),
                TeamMember(kind="group", role="observer", id=7, name="Service Desk"),
            ),
            linked_items=(
                LinkedItem(itemtype="Computer", item_id=7),
                LinkedItem(itemtype="NetworkEquipment", item_id=41),
                LinkedItem(itemtype="Software", item_id=2),
            ),
        )
    )
    assert (fields.glpi_group_id, fields.assigned_to) == (5, None)
    assert fields.ci == LinkedItem(itemtype="Computer", item_id=7)
    assert len(fields.warnings) == 3


def test_journal_mapping() -> None:
    snapshot = ticket(
        followups=(
            followup(12, "<p>Comms room open</p>", False, "ric.api", 30),
            followup(10, "<p>AT&amp;T engineer onsite at 1 PM.</p>", True, "r.kumar", 10),
            followup(
                11, "<p>[Store 01234 - Store Manager] Cabinet unlocked</p>", False, "ric.api", 20
            ),
            followup(13, "<p> </p>", False, "r.kumar", 40),
        )
    )
    entries = map_journal(snapshot, "ric.api")
    assert [e.glpi_followup_id for e in entries] == [10, 11, 12]
    note, store, own = entries
    assert (note.element, note.value, note.author_type, note.created_by) == (
        "work_notes",
        "AT&T engineer onsite at 1 PM.",
        "it_staff",
        "r.kumar",
    )
    assert not note.own_write
    assert (store.element, store.value, store.author_type, store.created_by, store.store_site) == (
        "comments",
        "Cabinet unlocked",
        "store_user",
        "Store Manager",
        "01234",
    )
    assert store.own_write
    assert own.own_write and own.author_type == "it_staff"


def test_text_helpers() -> None:
    sample = 'Cabinet unlocked & lights <OK>.\nManager said: "done"'
    assert html_to_text(text_to_html(sample)) == sample
    assert html_to_text("&lt;p&gt;Encoded&lt;/p&gt;") == "Encoded"
    assert html_to_text("<p>First</p><p>Second<br>line</p>") == "First\nSecond\nline"
    assert html_to_text(None) == ""
    prefixed = store_prefix("01234", "Store Manager") + "Access confirmed."
    assert split_store_prefix(prefixed) == ("01234", "Store Manager", "Access confirmed.")
    assert split_store_prefix("Plain") == (None, None, "Plain")
    assert split_description("Desc\nRIC details\nCaller: Store Manager - 01234\nOther: x") == (
        "Desc",
        {"caller": "Store Manager - 01234"},
    )
