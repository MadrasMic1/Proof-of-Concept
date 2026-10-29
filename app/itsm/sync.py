# app/itsm/sync.py
"""Make one RIC incident match its GLPI ticket (Stage 2, Phase 16).

    sync_ticket(factory, client, glpi_ticket_id, ...)   the whole sync of one ticket
    apply_snapshot(session, ...)                        the database part (testable)

Always compares a FRESH snapshot of the whole ticket with RIC, never the event
that triggered it: a repeated, late or missing webhook cannot leave RIC wrong.

    1. read the snapshot from GLPI (outside any transaction)
    2. one transaction: advisory lock on the GLPI ticket id, then
       - not an incident / deleted / location is not a RIC store -> ignored
       - not linked yet -> create the incident (RIC number: the ticket's external id
         when it is a valid, unused INC number, otherwise the next sequence number)
       - snapshot older than incidents.itsm_updated_at -> fields and state are left
         alone (an old snapshot never overwrites newer data; GLPI time vs GLPI time)
       - descriptive fields, then active fields (impact, urgency, assignment, ETA,
         state 1-3, hold reason), then resolve / close / cancel
       - a finished incident reopened in GLPI -> conflict (RIC never reopens)
       - follow-ups not stored yet -> journal entries (IT staff -> pending, so the
         worker runs the LLM); edited follow-ups -> text updated, not processed again
       - quiet=True (first sync after itsm_link): every entry written now is skipped
    3. after the commit: write the RIC number back to GLPI when it differs

Every write goes through app/services/incidents.py (rules, side effects, audit).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.actor import ActorContext
from app.db.session import session_scope
from app.itsm.errors import ItsmError, ItsmNotFound
from app.itsm.mapping import (
    IncidentFields,
    JournalFields,
    latest_solution,
    map_incident,
    map_journal,
)
from app.itsm.snapshot import TicketSnapshot
from app.models import AssignmentGroup, Device, Incident, IncidentJournal, NetworkElement, Store
from app.models.values import ACTIVE_INCIDENT_STATES, ActorType, AuditSource, IncidentState
from app.services.incident_rules import can_move, state_label
from app.services.incidents import (
    UNSET,
    IncidentUpdate,
    NewIncident,
    add_itsm_journal_entry,
    apply_itsm_details,
    cancel_incident,
    close_incident,
    create_incident,
    resolve_incident,
    skip_pending_entries,
    update_incident,
    update_itsm_journal_text,
)

logger = logging.getLogger("app.itsm")

SYNC_ACTOR_NAME = "itsm-sync"
SYNC_ACTOR = ActorContext.system(SYNC_ACTOR_NAME, str(AuditSource.ITSM_SYNC))
# Two-key advisory lock: (namespace, GLPI ticket id). Any fixed namespace number.
LOCK_NAMESPACE = 7_420_516
DEFAULT_CLOSE_CODE = "Solved"
DEFAULT_CLOSE_NOTES = "Resolved in GLPI."
DEFAULT_CANCEL_NOTE = "Cancelled in GLPI."


class Outcome:
    DONE = "done"
    IGNORED = "ignored"
    CONFLICT = "conflict"


class SyncClient(Protocol):
    """The part of GlpiClient the sync uses."""

    async def get_ticket_snapshot(self, ticket_id: int) -> TicketSnapshot: ...
    async def set_external_id(self, ticket_id: int, number: str) -> None: ...


@dataclass
class SyncResult:
    glpi_ticket_id: int
    outcome: str
    incident_number: str | None = None
    created: bool = False
    stale: bool = False
    changes: list[str] = field(default_factory=list)
    journal_added: int = 0
    journal_updated: int = 0
    skipped_entries: int = 0
    warnings: list[str] = field(default_factory=list)
    number_written_back: bool = False

    def summary(self) -> str:
        parts = [f"ticket {self.glpi_ticket_id}: {self.outcome}"]
        if self.incident_number:
            parts.append(self.incident_number + (" (created)" if self.created else ""))
        if self.stale:
            parts.append("stale snapshot: fields kept")
        if self.changes:
            parts.append("changed " + ", ".join(self.changes))
        if self.journal_added or self.journal_updated:
            parts.append(f"journal +{self.journal_added} ~{self.journal_updated}")
        if self.skipped_entries:
            parts.append(f"{self.skipped_entries} entries skipped (history)")
        if self.number_written_back:
            parts.append("number written to GLPI")
        return "; ".join(parts)


# --- references ----------------------------------------------------------------------


async def _lock_ticket(session: AsyncSession, glpi_ticket_id: int) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :ticket)"),
        {"namespace": LOCK_NAMESPACE, "ticket": glpi_ticket_id},
    )


async def _store_for_location(session: AsyncSession, location_id: int | None) -> uuid.UUID | None:
    if location_id is None:
        return None
    return await session.scalar(select(Store.id).where(Store.glpi_location_id == location_id))


async def _group_id(
    session: AsyncSession, fields: IncidentFields, warnings: list[str]
) -> uuid.UUID | None:
    """RIC group of the GLPI group; UNSET (keep the current one) when it is not linked."""
    if fields.glpi_group_id is None:
        return None
    group_id = await session.scalar(
        select(AssignmentGroup.id).where(AssignmentGroup.glpi_group_id == fields.glpi_group_id)
    )
    if group_id is None:
        warnings.append(
            f"GLPI group {fields.group_name!r} ({fields.glpi_group_id}) is not linked: kept"
        )
        return UNSET
    return group_id


async def _ci_ids(
    session: AsyncSession, fields: IncidentFields, store_id: uuid.UUID, warnings: list[str]
) -> tuple[uuid.UUID | None, uuid.UUID | None] | None:
    """(network element id, device id) of the linked GLPI asset in the store; (None, None)
    when the ticket has no asset; None (keep the current CI) when the asset is not linked."""
    if fields.ci is None:
        return None, None
    key = (fields.ci.itemtype, fields.ci.item_id)
    element_id = await session.scalar(
        select(NetworkElement.id).where(
            NetworkElement.glpi_itemtype == key[0],
            NetworkElement.glpi_item_id == key[1],
            NetworkElement.store_id == store_id,
        )
    )
    if element_id is not None:
        return element_id, None
    device_id = await session.scalar(
        select(Device.id).where(
            Device.glpi_itemtype == key[0],
            Device.glpi_item_id == key[1],
            Device.store_id == store_id,
        )
    )
    if device_id is not None:
        return None, device_id
    warnings.append(f"GLPI asset {key[0]} {key[1]} is not linked to this store: CI kept")
    return None


async def _number_for_new(session: AsyncSession, fields: IncidentFields) -> str | None:
    """The ticket's own RIC number when it is valid and unused; None = next in sequence."""
    if fields.number is None:
        return None
    used = await session.scalar(select(Incident.id).where(Incident.number == fields.number))
    return None if used is not None else fields.number


# --- the database part -----------------------------------------------------------------


def _resolver(snapshot: TicketSnapshot) -> ActorContext:
    """Resolution is an IT staff change (its close notes go to the worker, which asks
    the store to confirm the fix), made by the solution's author."""
    solution = latest_solution(snapshot)
    name = solution.author_name if solution and solution.author_name else SYNC_ACTOR_NAME
    return ActorContext(
        actor_type=str(ActorType.IT_STAFF), actor=name, source=str(AuditSource.ITSM_SYNC)
    )


def _details(
    fields: IncidentFields, ci: tuple[uuid.UUID | None, uuid.UUID | None] | None
) -> dict[str, object]:
    details: dict[str, object] = {
        "short_description": fields.short_description,
        "description": fields.description,
        "u_symptom": fields.u_symptom,
        "business_service": fields.business_service,
        "category": fields.category,
        "subcategory": fields.subcategory,
        "contact_type": fields.contact_type,
        "caller": fields.caller,
        "opened_at": fields.opened_at,
    }
    if fields.opened_by:
        details["opened_by"] = fields.opened_by
    if ci is not None:
        details["cmdb_ci_network_element_id"] = ci[0]
        details["cmdb_ci_device_id"] = ci[1]
    return details


async def _create(
    session: AsyncSession,
    fields: IncidentFields,
    store_id: uuid.UUID,
    group_id: uuid.UUID | None,
    ci: tuple[uuid.UUID | None, uuid.UUID | None] | None,
) -> Incident:
    element_id, device_id = ci if ci is not None else (None, None)
    return await create_incident(
        session,
        SYNC_ACTOR,
        store_id,
        NewIncident(
            short_description=fields.short_description,
            category=fields.category,
            impact=fields.impact,
            urgency=fields.urgency,
            contact_type=fields.contact_type,
            description=fields.description,
            u_symptom=fields.u_symptom,
            business_service=fields.business_service,
            subcategory=fields.subcategory,
            caller=fields.caller,
            network_element_id=element_id,
            device_id=device_id,
            assignment_group_id=None if group_id is UNSET else group_id,
            assigned_to=fields.assigned_to,
            u_resolution_eta=fields.u_resolution_eta,
            u_eta_note=fields.u_eta_note,
        ),
        fields.opened_at,
        commit=False,
        number=await _number_for_new(session, fields),
        opened_by=fields.opened_by or "glpi",
    )


def _active_fields(incident: Incident) -> tuple[object, ...]:
    return (
        incident.state,
        incident.hold_reason,
        incident.impact,
        incident.urgency,
        incident.assignment_group_id,
        incident.assigned_to,
        incident.u_resolution_eta,
        incident.u_eta_note,
    )


async def _apply_state(
    session: AsyncSession,
    incident: Incident,
    fields: IncidentFields,
    snapshot: TicketSnapshot,
    group_id: uuid.UUID | None,
    result: SyncResult,
) -> None:
    """Active fields and state. Sets result.outcome to conflict on a reopen."""
    current, target = incident.state, fields.state
    if current in ACTIVE_INCIDENT_STATES:
        state = target if target in ACTIVE_INCIDENT_STATES else current
        if state != current and not can_move(current, state):
            result.warnings.append(
                f"GLPI state {state_label(target)} cannot follow {state_label(current)}: kept"
            )
            state = current
        before = _active_fields(incident)
        await update_incident(
            session,
            SYNC_ACTOR,
            incident.id,
            IncidentUpdate(
                state=state,
                hold_reason=fields.hold_reason if state == IncidentState.ON_HOLD else UNSET,
                impact=fields.impact,
                urgency=fields.urgency,
                assignment_group_id=group_id,
                assigned_to=fields.assigned_to,
                u_resolution_eta=fields.u_resolution_eta,
                u_eta_note=fields.u_eta_note,
            ),
            fields.itsm_updated_at,
            commit=False,
        )
        if before != _active_fields(incident):
            result.changes.append("state/assignment/priority/eta")

    current = incident.state
    if target == current or (
        target in ACTIVE_INCIDENT_STATES and current in ACTIVE_INCIDENT_STATES
    ):
        return
    resolved_at = max(fields.resolved_at or fields.itsm_updated_at, incident.opened_at)
    if current in ACTIVE_INCIDENT_STATES and target in (
        IncidentState.RESOLVED,
        IncidentState.CLOSED,
    ):
        await resolve_incident(
            session,
            _resolver(snapshot),
            incident.id,
            fields.close_code or DEFAULT_CLOSE_CODE,
            fields.close_notes or DEFAULT_CLOSE_NOTES,
            resolved_at,
            commit=False,
        )
        result.changes.append("resolved")
        current = incident.state
    if current == IncidentState.RESOLVED and target == IncidentState.CLOSED:
        closed_at = max(fields.closed_at or fields.itsm_updated_at, incident.resolved_at)
        await close_incident(session, SYNC_ACTOR, incident.id, closed_at, commit=False)
        result.changes.append("closed")
        return
    if current in ACTIVE_INCIDENT_STATES and target == IncidentState.CANCELED:
        closed_at = max(fields.closed_at or fields.itsm_updated_at, incident.opened_at)
        await cancel_incident(
            session,
            SYNC_ACTOR,
            incident.id,
            fields.close_notes or DEFAULT_CANCEL_NOTE,
            closed_at,
            commit=False,
        )
        result.changes.append("cancelled")
        return
    if incident.state != target:
        result.outcome = Outcome.CONFLICT
        result.warnings.append(
            f"GLPI says {state_label(target)} but RIC is {state_label(incident.state)}: "
            "RIC does not reopen or change finished incidents"
        )


async def _apply_journal(
    session: AsyncSession,
    incident: Incident,
    journal: list[JournalFields],
    fields: IncidentFields,
    now: datetime,
    quiet: bool,
    result: SyncResult,
) -> None:
    stored = {
        row.glpi_followup_id: (row.id, row.value)
        for row in (
            await session.execute(
                select(
                    IncidentJournal.glpi_followup_id, IncidentJournal.id, IncidentJournal.value
                ).where(
                    IncidentJournal.incident_id == incident.id,
                    IncidentJournal.glpi_followup_id.is_not(None),
                )
            )
        ).all()
    }
    for entry in journal:
        if entry.glpi_followup_id in stored:
            entry_id, value = stored[entry.glpi_followup_id]
            if value != entry.value:
                await update_itsm_journal_text(
                    session, SYNC_ACTOR, entry_id, entry.value, commit=False
                )
                result.journal_updated += 1
            continue
        # A follow-up in another incident's journal would break the unique id: skip it.
        elsewhere = await session.scalar(
            select(IncidentJournal.id).where(
                IncidentJournal.glpi_followup_id == entry.glpi_followup_id
            )
        )
        if elsewhere is not None:
            result.warnings.append(
                f"follow-up {entry.glpi_followup_id} belongs to another incident: skipped"
            )
            continue
        await add_itsm_journal_entry(
            session,
            SYNC_ACTOR,
            incident.id,
            element=entry.element,
            value=entry.value,
            author_type=entry.author_type,
            created_by=entry.created_by,
            created_at=entry.created_at or fields.itsm_updated_at,
            glpi_followup_id=entry.glpi_followup_id,
            skip=quiet or entry.own_write,
            now=now,
            commit=False,
        )
        result.journal_added += 1


async def apply_snapshot(
    session: AsyncSession,
    snapshot: TicketSnapshot,
    *,
    now: datetime,
    api_username: str,
    quiet: bool = False,
) -> SyncResult:
    """The database part of one sync, inside the caller's transaction (never commits)."""
    fields = map_incident(snapshot)
    journal = map_journal(snapshot, api_username)
    result = SyncResult(glpi_ticket_id=snapshot.id, outcome=Outcome.DONE)
    result.warnings.extend(fields.warnings)
    await _lock_ticket(session, snapshot.id)

    incident = await session.scalar(select(Incident).where(Incident.glpi_ticket_id == snapshot.id))
    if fields.is_deleted or not fields.is_incident:
        result.outcome = Outcome.IGNORED
        why = "deleted in GLPI" if fields.is_deleted else "not an incident"
        result.warnings.append(f"ticket is {why}" + (": RIC incident kept" if incident else ""))
        result.incident_number = incident.number if incident else None
        return result

    store_id = await _store_for_location(session, fields.glpi_location_id)
    if incident is None and fields.number is not None:
        unlinked = await session.scalar(
            select(Incident.id).where(
                Incident.number == fields.number, Incident.glpi_ticket_id.is_(None)
            )
        )
        if unlinked is not None:
            result.outcome = Outcome.CONFLICT
            result.incident_number = fields.number
            result.warnings.append(
                f"RIC incident {fields.number} exists but is not linked to this ticket: "
                "run scripts.itsm_link (nothing created)"
            )
            return result
    if incident is None and store_id is None:
        result.outcome = Outcome.IGNORED
        result.warnings.append(
            f"location {fields.location_name!r} ({fields.glpi_location_id}) is not a RIC store"
        )
        return result
    if incident is not None and store_id != incident.store_id:
        result.warnings.append("GLPI location is not the incident's store: store kept")
    store_id = incident.store_id if incident is not None else store_id

    before_ids: set[uuid.UUID] = set()
    if incident is not None:
        before_ids = set(
            (
                await session.scalars(
                    select(IncidentJournal.id).where(IncidentJournal.incident_id == incident.id)
                )
            ).all()
        )

    group_id = await _group_id(session, fields, result.warnings)
    ci = await _ci_ids(session, fields, store_id, result.warnings)

    if incident is None:
        incident = await _create(session, fields, store_id, group_id, ci)
        result.created = True
        result.changes.append("created")
        await apply_itsm_details(
            session,
            SYNC_ACTOR,
            incident.id,
            {},
            now,
            itsm_updated_at=fields.itsm_updated_at,
            glpi_ticket_id=snapshot.id,
            commit=False,
        )
        await _apply_state(session, incident, fields, snapshot, group_id, result)
    elif incident.itsm_updated_at is not None and fields.itsm_updated_at < incident.itsm_updated_at:
        result.stale = True
        await apply_itsm_details(
            session,
            SYNC_ACTOR,
            incident.id,
            {},
            now,
            itsm_updated_at=fields.itsm_updated_at,
            commit=False,
        )
    else:
        details = _details(fields, ci)
        if incident.resolved_at is not None and fields.opened_at > incident.resolved_at:
            details.pop("opened_at")
            result.warnings.append("GLPI opening time is after the resolution time: kept")
        changed = await apply_itsm_details(
            session,
            SYNC_ACTOR,
            incident.id,
            details,
            now,
            itsm_updated_at=fields.itsm_updated_at,
            commit=False,
        )
        result.changes.extend(sorted(changed))
        await _apply_state(session, incident, fields, snapshot, group_id, result)

    await _apply_journal(session, incident, journal, fields, now, quiet, result)
    if quiet:
        new_ids = (
            await session.scalars(
                select(IncidentJournal.id).where(IncidentJournal.incident_id == incident.id)
            )
        ).all()
        result.skipped_entries = await skip_pending_entries(
            session, [i for i in new_ids if i not in before_ids], now, commit=False
        )
    result.incident_number = incident.number
    return result


# --- the whole sync -------------------------------------------------------------------


async def sync_ticket(
    factory: async_sessionmaker[AsyncSession],
    client: SyncClient,
    glpi_ticket_id: int,
    *,
    now: datetime,
    api_username: str,
    quiet: bool = False,
) -> SyncResult:
    """Read the ticket, apply it in one transaction, then write the RIC number back.

    Raises ItsmUnavailable / ItsmRejected from the read (the caller retries or fails
    the inbox row); a ticket that no longer exists is 'ignored'.
    """
    try:
        snapshot = await client.get_ticket_snapshot(glpi_ticket_id)
    except ItsmNotFound:
        return SyncResult(glpi_ticket_id, Outcome.IGNORED, warnings=["ticket not found in GLPI"])
    async with session_scope(factory) as session:
        result = await apply_snapshot(
            session, snapshot, now=now, api_username=api_username, quiet=quiet
        )
    number = result.incident_number
    if result.outcome != Outcome.IGNORED and number and snapshot.external_id != number:
        try:
            await client.set_external_id(glpi_ticket_id, number)
            result.number_written_back = True
        except ItsmError as exc:
            result.warnings.append(f"RIC number not written to GLPI (next sync retries): {exc}")
    for warning in result.warnings:
        logger.warning("itsm sync: %s", warning, extra={"glpiTicketId": glpi_ticket_id})
    return result
