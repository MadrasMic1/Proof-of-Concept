# app/itsm/tickets.py
"""Create the GLPI ticket of a NEW RIC incident (RIC -> GLPI), used when a store
manager confirms an incident draft in the chat (Agentic Phase 8).

Inside the caller's transaction, right after the RIC incident is created (never commits):
    1. the store must be linked to a GLPI Location (scripts.itsm_link), else nothing
    2. POST Ticket (API V1): name, content, type incident, urgency / impact, location,
       assigned group (when linked), category (when GLPI has a matching one) and
       externalid = the RIC number
    3. link the CI (Item_Ticket) when the device / element is linked to a GLPI asset;
       a failure here is a warning (the ticket exists)
    4. take the sync's advisory lock on the new ticket id and set incident.glpi_ticket_id
The lock makes a webhook-triggered sync of the new ticket wait for the caller's commit,
then it finds the incident by glpi_ticket_id. If the caller's commit fails after step 2,
the sync creates the RIC incident from the ticket (its external id is the RIC number), so
no GLPI ticket is ever left without a RIC incident.

Levels: RIC 1 high / 2 medium / 3 low -> GLPI 4 high / 3 medium / 2 low (GLPI scale 1-5).
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.itsm.sync import LOCK_NAMESPACE
from app.models import AssignmentGroup, Device, Incident, NetworkElement, Store

TICKET_TYPE_INCIDENT = 1
GLPI_LEVEL = {1: 4, 2: 3, 3: 2}


class StoreNotLinkedError(Exception):
    """The store has no GLPI Location: the ticket cannot be placed in GLPI."""


class TicketClient(Protocol):
    """The part of GlpiClient that creating a ticket uses."""

    async def create_ticket(self, fields: dict[str, object]) -> int: ...
    async def link_item(self, ticket_id: int, itemtype: str, item_id: int) -> None: ...
    async def find_category_id(self, category: str, subcategory: str | None) -> int | None: ...


@dataclass
class PushResult:
    ticket_id: int
    warnings: list[str] = field(default_factory=list)


def ticket_content_html(
    description: str, *, site_code: str, store_name: str, raised_by: str, number: str
) -> str:
    """The ticket body: who raised it and where, then the description (escaped)."""
    head = (
        f"[Raised from RIC by {html.escape(raised_by)} - store {html.escape(site_code)} "
        f"{html.escape(store_name)} - RIC {html.escape(number)}]"
    )
    paragraphs = [
        "<br>".join(html.escape(line) for line in block.splitlines())
        for block in description.split("\n\n")
        if block.strip()
    ]
    return "".join(f"<p>{part}</p>" for part in [head, *paragraphs])


async def _ci(
    session: AsyncSession, incident: Incident
) -> tuple[str | None, int | None, str | None]:
    """(GLPI itemtype, GLPI item id, CI name) of the incident's device / element."""
    if incident.cmdb_ci_device_id is not None:
        row = (
            await session.execute(
                select(Device.glpi_itemtype, Device.glpi_item_id, Device.name).where(
                    Device.id == incident.cmdb_ci_device_id
                )
            )
        ).one()
        return row.glpi_itemtype, row.glpi_item_id, row.name
    if incident.cmdb_ci_network_element_id is not None:
        row = (
            await session.execute(
                select(
                    NetworkElement.glpi_itemtype, NetworkElement.glpi_item_id, NetworkElement.name
                ).where(NetworkElement.id == incident.cmdb_ci_network_element_id)
            )
        ).one()
        return row.glpi_itemtype, row.glpi_item_id, row.name
    return None, None, None


async def push_new_incident(
    session: AsyncSession, client: TicketClient, incident: Incident, *, raised_by: str
) -> PushResult:
    store = (
        await session.execute(
            select(Store.site_code, Store.name, Store.glpi_location_id).where(
                Store.id == incident.store_id
            )
        )
    ).one()
    if store.glpi_location_id is None:
        raise StoreNotLinkedError(
            f"store {store.site_code} is not linked to GLPI (run scripts.itsm_link)"
        )
    warnings: list[str] = []
    fields: dict[str, object] = {
        "name": incident.short_description,
        "content": ticket_content_html(
            incident.description or incident.short_description,
            site_code=store.site_code,
            store_name=store.name,
            raised_by=raised_by,
            number=incident.number,
        ),
        "type": TICKET_TYPE_INCIDENT,
        "urgency": GLPI_LEVEL.get(incident.urgency, 3),
        "impact": GLPI_LEVEL.get(incident.impact, 3),
        "locations_id": store.glpi_location_id,
        "externalid": incident.number,
    }
    if incident.assignment_group_id is not None:
        group_id = await session.scalar(
            select(AssignmentGroup.glpi_group_id).where(
                AssignmentGroup.id == incident.assignment_group_id
            )
        )
        if group_id is not None:
            fields["_groups_id_assign"] = group_id
        else:
            warnings.append("the assignment group is not linked to GLPI: ticket unassigned")
    category_id = await client.find_category_id(incident.category, incident.subcategory)
    if category_id is not None:
        fields["itilcategories_id"] = category_id
    ticket_id = await client.create_ticket(fields)
    itemtype, item_id, ci_name = await _ci(session, incident)
    if itemtype and item_id:
        try:
            await client.link_item(ticket_id, itemtype, item_id)
        except Exception as exc:  # the ticket exists; the CI link is secondary
            warnings.append(f"{ci_name} could not be linked to the GLPI ticket: {exc}")
    elif ci_name is not None:
        warnings.append(f"{ci_name} is not linked to a GLPI asset: no CI on the ticket")
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:namespace, :ticket)"),
        {"namespace": LOCK_NAMESPACE, "ticket": ticket_id},
    )
    incident.glpi_ticket_id = ticket_id
    await session.flush()
    return PushResult(ticket_id, warnings)
