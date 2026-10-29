# app/itsm/link.py
"""Link RIC rows to the GLPI objects that already exist for them (Phase 15).

Nothing is created in GLPI and no incident content is copied: linking only fills
the glpi_* id columns, queues one itsm_inbox row per incident ticket of a RIC
store (its first sync) and sets the starting watermark of the reconciliation job.

    store           <- GLPI Location whose name starts with "<site_code> - "
    assignment group <- GLPI Group with the same name
    element / device <- GLPI asset with the same name at the store's location
    incident        <- GLPI Ticket whose external_id is the RIC number
    journal entry   <- GLPI follow-up of the linked ticket with the same kind
                       (work note = private, comment = public) and the same text

A match must be unique on both sides. Anything else is reported and left as it
is: no match, several matches (ambiguous), the GLPI object is already linked to
another row, or the row is already linked to a different GLPI object (changed
only with relink=True). Running it again changes nothing (everything "already").

plan_links() is pure; load_ric_state() reads, apply_plan() writes (caller's
transaction, never commits); run_link() does the whole run.
"""

from __future__ import annotations

import json
import re
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Hashable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import session_scope
from app.itsm.errors import ItsmNotFound
from app.itsm.mapping import JournalFields, map_journal
from app.itsm.snapshot import GlpiAsset, GlpiNamed, GlpiTicketRef, TicketSnapshot
from app.models import (
    AssignmentGroup,
    Device,
    Incident,
    IncidentJournal,
    NetworkElement,
    Store,
)
from app.models.values import InboxSource, JournalElement

RECONCILE_JOB = "itsm_reconcile"
TICKET_TYPE_INCIDENT = 1
LOCATION_SITE = re.compile(r"^(\d{5}) - ")
# Any fixed number, different from the event processor's key.
ADVISORY_LOCK_KEY = 7_420_515
LINKED_ELEMENTS = (str(JournalElement.WORK_NOTES), str(JournalElement.COMMENTS))
KINDS = ("stores", "groups", "assets", "incidents", "journal")

QUEUE_SQL = text(
    """
    INSERT INTO itsm_inbox (glpi_ticket_id, source, events)
    VALUES (:ticket, :source, CAST(:events AS jsonb))
    ON CONFLICT (glpi_ticket_id) WHERE status = 'pending' DO NOTHING
    RETURNING id
    """
)
SYNC_STATE_SQL = text(
    """
    INSERT INTO itsm_sync_state (job_name, interval_seconds, next_run_at, watermark)
    VALUES (:job, :interval, :now, :watermark)
    ON CONFLICT (job_name) DO UPDATE
    SET interval_seconds = EXCLUDED.interval_seconds,
        watermark = GREATEST(itsm_sync_state.watermark, EXCLUDED.watermark)
    RETURNING watermark, next_run_at
    """
)


class LinkBusyError(RuntimeError):
    """Another link run holds the lock."""


class LinkClient(Protocol):
    """The part of GlpiClient that linking uses."""

    async def list_locations(self) -> list[GlpiNamed]: ...
    async def list_groups(self) -> list[GlpiNamed]: ...
    async def list_assets(self) -> list[GlpiAsset]: ...
    async def list_tickets(self) -> list[GlpiTicketRef]: ...
    async def get_ticket_snapshot(self, ticket_id: int) -> TicketSnapshot: ...


# --- inputs ----------------------------------------------------------------------------


@dataclass(frozen=True)
class GlpiInventory:
    locations: list[GlpiNamed]
    groups: list[GlpiNamed]
    assets: list[GlpiAsset]
    tickets: list[GlpiTicketRef]


@dataclass(frozen=True)
class StoreRow:
    id: uuid.UUID
    site_code: str
    glpi_location_id: int | None


@dataclass(frozen=True)
class GroupRow:
    id: uuid.UUID
    name: str
    glpi_group_id: int | None


@dataclass(frozen=True)
class AssetRow:
    id: uuid.UUID
    table: str  # "network_element" or "device"
    store_id: uuid.UUID
    name: str
    glpi_itemtype: str | None
    glpi_item_id: int | None


@dataclass(frozen=True)
class IncidentRow:
    id: uuid.UUID
    number: str
    store_id: uuid.UUID
    glpi_ticket_id: int | None


@dataclass(frozen=True)
class JournalRow:
    id: uuid.UUID
    incident_id: uuid.UUID
    element: str
    value: str
    created_at: datetime
    glpi_followup_id: int | None


@dataclass(frozen=True)
class RicState:
    stores: list[StoreRow]
    groups: list[GroupRow]
    assets: list[AssetRow]
    incidents: list[IncidentRow]
    journal: list[JournalRow]


# --- result ----------------------------------------------------------------------------


@dataclass
class KindReport:
    linked: int = 0
    already: int = 0
    no_match: int = 0
    ambiguous: int = 0
    conflict: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def problems(self) -> int:
        return self.ambiguous + self.conflict


@dataclass
class LinkPlan:
    store_links: dict[uuid.UUID, int] = field(default_factory=dict)
    group_links: dict[uuid.UUID, int] = field(default_factory=dict)
    element_links: dict[uuid.UUID, tuple[str, int]] = field(default_factory=dict)
    device_links: dict[uuid.UUID, tuple[str, int]] = field(default_factory=dict)
    incident_links: dict[uuid.UUID, int] = field(default_factory=dict)
    journal_links: dict[uuid.UUID, int] = field(default_factory=dict)
    reports: dict[str, KindReport] = field(default_factory=lambda: {k: KindReport() for k in KINDS})
    incident_tickets: dict[uuid.UUID, int] = field(default_factory=dict)
    queue: list[int] = field(default_factory=list)
    watermark: datetime | None = None
    ric_only_incidents: list[str] = field(default_factory=list)
    glpi_only_tickets: list[int] = field(default_factory=list)
    glpi_only_followups: int = 0

    @property
    def problems(self) -> int:
        return sum(report.problems for report in self.reports.values())


@dataclass
class LinkResult:
    plan: LinkPlan
    inventory: GlpiInventory
    queued: int = 0
    already_queued: int = 0
    watermark: datetime | None = None
    next_run_at: datetime | None = None
    dry_run: bool = False
    skipped_tickets: list[int] = field(default_factory=list)


# --- matching ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Row:
    id: uuid.UUID
    key: Hashable | None
    current: Hashable | None
    label: str


def _match(
    report: KindReport,
    rows: list[_Row],
    candidates: dict[Hashable, list[Hashable]],
    relink: bool,
) -> tuple[dict[uuid.UUID, Hashable], dict[uuid.UUID, Hashable | None]]:
    """(changes, final): changes = rows whose link is set or changed; final = every row's
    link after the run (None = not linked)."""
    key_counts = Counter(row.key for row in rows if row.key is not None)
    holders = {row.current: row.id for row in rows if row.current is not None}
    changes: dict[uuid.UUID, Hashable] = {}
    final: dict[uuid.UUID, Hashable | None] = {row.id: row.current for row in rows}
    for row in rows:
        found = candidates.get(row.key, []) if row.key is not None else []
        if row.key is not None and key_counts[row.key] > 1:
            report.ambiguous += 1
            report.details.append(f"{row.label}: {key_counts[row.key]} RIC rows have this key")
            continue
        if not found:
            report.no_match += 1
            kept = f" (existing link {row.current} kept)" if row.current is not None else ""
            report.details.append(f"{row.label}: no GLPI match{kept}")
            continue
        if len(found) > 1:
            report.ambiguous += 1
            report.details.append(
                f"{row.label}: {len(found)} GLPI matches {sorted(found, key=str)}"
            )
            continue
        target = found[0]
        if row.current == target:
            report.already += 1
            continue
        holder = holders.get(target)
        if holder is not None and holder != row.id:
            report.conflict += 1
            report.details.append(f"{row.label}: GLPI {target} is already linked to another row")
            continue
        if row.current is not None and not relink:
            report.conflict += 1
            report.details.append(
                f"{row.label}: linked to {row.current}, GLPI match is {target} (use --relink)"
            )
            continue
        if row.current is not None:
            holders.pop(row.current, None)
        holders[target] = row.id
        changes[row.id] = target
        final[row.id] = target
        report.linked += 1
    return changes, final


def _index(pairs: Iterable[tuple[Hashable, Hashable]]) -> dict[Hashable, list[Hashable]]:
    index: dict[Hashable, list[Hashable]] = defaultdict(list)
    for key, value in pairs:
        index[key].append(value)
    return index


def normalize_text(value: str) -> str:
    """Compare journal text and follow-up text ignoring spacing and line breaks."""
    return " ".join(value.split())


def _plan_journal(
    plan: LinkPlan,
    ric: RicState,
    followups: dict[int, list[JournalFields]],
) -> None:
    """Pair unlinked work notes / comments with unlinked follow-ups of the same ticket:
    same kind and text, oldest with oldest. System entries are never linked."""
    report = plan.reports["journal"]
    linked_fids = {row.glpi_followup_id for row in ric.journal if row.glpi_followup_id is not None}
    by_incident: dict[uuid.UUID, list[JournalRow]] = defaultdict(list)
    for row in ric.journal:
        if row.element in LINKED_ELEMENTS:
            by_incident[row.incident_id].append(row)
    for incident_id, ticket_id in plan.incident_tickets.items():
        if ticket_id not in followups:
            continue
        queues: dict[tuple[str, str], deque[JournalFields]] = defaultdict(deque)
        for entry in followups[ticket_id]:
            if entry.glpi_followup_id not in linked_fids:
                queues[(entry.element, normalize_text(entry.value))].append(entry)
        for row in sorted(by_incident[incident_id], key=lambda r: (r.created_at, str(r.id))):
            if row.glpi_followup_id is not None:
                report.already += 1
                continue
            queue = queues.get((row.element, normalize_text(row.value)))
            if not queue:
                report.no_match += 1
                continue
            entry = queue.popleft()
            plan.journal_links[row.id] = entry.glpi_followup_id
            linked_fids.add(entry.glpi_followup_id)
            report.linked += 1
        plan.glpi_only_followups += sum(len(queue) for queue in queues.values())


def plan_links(
    ric: RicState,
    glpi: GlpiInventory,
    followups: dict[int, list[JournalFields]] | None = None,
    *,
    relink: bool = False,
) -> LinkPlan:
    """Decide every link (pure: no I/O)."""
    plan = LinkPlan()
    tickets = [ticket for ticket in glpi.tickets if not ticket.is_deleted]

    # stores <- locations named "<site_code> - ..."
    sites = []
    for location in glpi.locations:
        match = LOCATION_SITE.match(location.name)
        if match:
            sites.append((match.group(1), location.id))
    changes, store_final = _match(
        plan.reports["stores"],
        [_Row(s.id, s.site_code, s.glpi_location_id, f"store {s.site_code}") for s in ric.stores],
        _index(sites),
        relink,
    )
    plan.store_links = {key: int(value) for key, value in changes.items()}
    store_location = {store_id: loc for store_id, loc in store_final.items() if loc is not None}
    site_of = {s.id: s.site_code for s in ric.stores}

    # assignment groups <- groups with the same name
    changes, _ = _match(
        plan.reports["groups"],
        [_Row(g.id, g.name, g.glpi_group_id, f"group {g.name!r}") for g in ric.groups],
        _index((group.name, group.id) for group in glpi.groups),
        relink,
    )
    plan.group_links = {key: int(value) for key, value in changes.items()}

    # elements and devices <- assets with the same name at the store's location
    asset_rows = []
    for asset in ric.assets:
        location = store_location.get(asset.store_id)
        current = (
            (asset.glpi_itemtype, asset.glpi_item_id) if asset.glpi_item_id is not None else None
        )
        label = f"{asset.table.replace('_', ' ')} {site_of.get(asset.store_id, '?')}/{asset.name}"
        key = (location, asset.name) if location is not None else None
        asset_rows.append(_Row(asset.id, key, current, label))
    changes, _ = _match(
        plan.reports["assets"],
        asset_rows,
        _index(((a.location_id, a.name), (a.itemtype, a.id)) for a in glpi.assets),
        relink,
    )
    table_of = {asset.id: asset.table for asset in ric.assets}
    for row_id, target in changes.items():
        links = plan.element_links if table_of[row_id] == "network_element" else plan.device_links
        links[row_id] = target

    # incidents <- tickets whose external id is the RIC number
    changes, incident_final = _match(
        plan.reports["incidents"],
        [_Row(i.id, i.number, i.glpi_ticket_id, f"incident {i.number}") for i in ric.incidents],
        _index((t.external_id, t.id) for t in tickets if t.external_id),
        relink,
    )
    plan.incident_links = {key: int(value) for key, value in changes.items()}
    plan.incident_tickets = {
        incident_id: int(ticket)
        for incident_id, ticket in incident_final.items()
        if ticket is not None
    }
    plan.ric_only_incidents = sorted(
        i.number for i in ric.incidents if i.id not in plan.incident_tickets
    )

    # journal entries <- follow-ups of the linked ticket
    _plan_journal(plan, ric, followups or {})

    # first sync of every incident ticket at a RIC store; watermark = newest GLPI date_mod
    store_locations = set(store_location.values())
    plan.queue = sorted(
        t.id for t in tickets if t.type == TICKET_TYPE_INCIDENT and t.location_id in store_locations
    )
    linked_tickets = set(plan.incident_tickets.values())
    plan.glpi_only_tickets = [
        ticket_id for ticket_id in plan.queue if ticket_id not in linked_tickets
    ]
    stamps = [t.date_mod for t in tickets if t.date_mod is not None]
    plan.watermark = max(stamps) if stamps else None
    return plan


# --- database --------------------------------------------------------------------------


async def load_ric_state(session: AsyncSession) -> RicState:
    stores = [
        StoreRow(*row)
        for row in (
            await session.execute(
                select(Store.id, Store.site_code, Store.glpi_location_id).order_by(Store.site_code)
            )
        ).all()
    ]
    groups = [
        GroupRow(*row)
        for row in (
            await session.execute(
                select(
                    AssignmentGroup.id, AssignmentGroup.name, AssignmentGroup.glpi_group_id
                ).order_by(AssignmentGroup.name)
            )
        ).all()
    ]
    assets: list[AssetRow] = []
    for table, model in (("network_element", NetworkElement), ("device", Device)):
        rows = await session.execute(
            select(
                model.id, model.store_id, model.name, model.glpi_itemtype, model.glpi_item_id
            ).order_by(model.store_id, model.name)
        )
        assets.extend(AssetRow(row[0], table, *row[1:]) for row in rows.all())
    incidents = [
        IncidentRow(*row)
        for row in (
            await session.execute(
                select(
                    Incident.id, Incident.number, Incident.store_id, Incident.glpi_ticket_id
                ).order_by(Incident.number)
            )
        ).all()
    ]
    journal = [
        JournalRow(*row)
        for row in (
            await session.execute(
                select(
                    IncidentJournal.id,
                    IncidentJournal.incident_id,
                    IncidentJournal.element,
                    IncidentJournal.value,
                    IncidentJournal.created_at,
                    IncidentJournal.glpi_followup_id,
                )
            )
        ).all()
    ]
    return RicState(stores, groups, assets, incidents, journal)


async def _set_links(
    session: AsyncSession, model: Any, links: dict[uuid.UUID, Any], columns: tuple[str, ...]
) -> None:
    """Clear the changed rows first, then set them: a changed link may take an id
    another changed row gives up in the same run (unique columns)."""
    if not links:
        return
    ids = list(links)
    await session.execute(
        update(model).where(model.id.in_(ids)).values({column: None for column in columns})
    )
    rows = []
    for row_id, value in links.items():
        values = value if isinstance(value, tuple) else (value,)
        rows.append({"id": row_id, **dict(zip(columns, values, strict=True))})
    await session.execute(update(model), rows)


async def acquire_link_lock(session: AsyncSession) -> None:
    locked = await session.scalar(
        text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY}
    )
    if not locked:
        raise LinkBusyError("another itsm_link run is in progress")


async def apply_plan(
    session: AsyncSession,
    plan: LinkPlan,
    *,
    now: datetime,
    interval_seconds: int,
) -> tuple[int, int, datetime | None, datetime | None]:
    """Write the links, queue the first syncs and set the watermark. Never commits.
    Returns (queued, already pending, watermark, next_run_at)."""
    await _set_links(session, Store, plan.store_links, ("glpi_location_id",))
    await _set_links(session, AssignmentGroup, plan.group_links, ("glpi_group_id",))
    asset_columns = ("glpi_itemtype", "glpi_item_id")
    await _set_links(session, NetworkElement, plan.element_links, asset_columns)
    await _set_links(session, Device, plan.device_links, asset_columns)
    await _set_links(session, Incident, plan.incident_links, ("glpi_ticket_id",))
    await _set_links(session, IncidentJournal, plan.journal_links, ("glpi_followup_id",))

    events = json.dumps([{"event": "link", "at": now.isoformat()}])
    queued = 0
    for ticket_id in plan.queue:
        inserted = await session.scalar(
            QUEUE_SQL, {"ticket": ticket_id, "source": str(InboxSource.LINK), "events": events}
        )
        queued += inserted is not None
    state = (
        await session.execute(
            SYNC_STATE_SQL,
            {
                "job": RECONCILE_JOB,
                "interval": interval_seconds,
                "now": now,
                "watermark": plan.watermark,
            },
        )
    ).one()
    await session.flush()
    return queued, len(plan.queue) - queued, state.watermark, state.next_run_at


# --- the whole run ---------------------------------------------------------------------


async def read_glpi_inventory(client: LinkClient) -> GlpiInventory:
    return GlpiInventory(
        locations=await client.list_locations(),
        groups=await client.list_groups(),
        assets=await client.list_assets(),
        tickets=await client.list_tickets(),
    )


async def run_link(
    factory: async_sessionmaker[AsyncSession],
    client: LinkClient,
    *,
    now: datetime,
    interval_seconds: int,
    api_username: str,
    relink: bool = False,
    dry_run: bool = False,
) -> LinkResult:
    """Read GLPI, then link in one transaction. GLPI calls happen outside the
    write transaction; the plan is recomputed inside it from fresh RIC rows."""
    inventory = await read_glpi_inventory(client)
    async with factory() as session:
        preview = plan_links(await load_ric_state(session), inventory, relink=relink)
    followups: dict[int, list[JournalFields]] = {}
    skipped: list[int] = []
    for ticket_id in sorted(set(preview.incident_tickets.values())):
        try:
            snapshot = await client.get_ticket_snapshot(ticket_id)
        except ItsmNotFound:
            skipped.append(ticket_id)
            continue
        followups[ticket_id] = map_journal(snapshot, api_username)

    async with session_scope(factory) as session:
        await acquire_link_lock(session)
        plan = plan_links(await load_ric_state(session), inventory, followups, relink=relink)
        result = LinkResult(
            plan=plan, inventory=inventory, dry_run=dry_run, skipped_tickets=skipped
        )
        if dry_run:
            result.already_queued = 0
            result.watermark = plan.watermark
            return result
        queued, already, watermark, next_run_at = await apply_plan(
            session, plan, now=now, interval_seconds=interval_seconds
        )
    result.queued, result.already_queued = queued, already
    result.watermark, result.next_run_at = watermark, next_run_at
    return result
