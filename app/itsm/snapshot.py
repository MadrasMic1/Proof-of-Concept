# app/itsm/snapshot.py
"""One GLPI ticket as read from the API, in GLPI terms (no RIC values yet).

The client builds a TicketSnapshot; app.itsm.mapping turns it into RIC values.
Times are already UTC. Text fields are as GLPI stores them (HTML for content).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class TeamMember:
    """An actor of the ticket. kind: 'user' or 'group'; role: 'assigned', 'requester',
    'observer' (normalised to lower case; 'Assigned to' becomes 'assigned')."""

    kind: str
    role: str
    id: int
    name: str


@dataclass(frozen=True)
class FollowupSnapshot:
    id: int
    content_html: str
    is_private: bool
    author_id: int | None
    author_name: str | None
    created_at: datetime | None
    updated_at: datetime | None


@dataclass(frozen=True)
class SolutionSnapshot:
    id: int
    content_html: str
    type_name: str | None
    author_name: str | None
    created_at: datetime | None


@dataclass(frozen=True)
class LinkedItem:
    """An asset linked to the ticket (the CI): GLPI itemtype and id."""

    itemtype: str
    item_id: int


@dataclass(frozen=True)
class TicketSnapshot:
    id: int
    name: str
    content_html: str
    status: int
    type: int
    urgency: int
    impact: int
    is_deleted: bool
    external_id: str | None
    location_id: int | None
    location_name: str | None
    request_type_name: str | None
    category_name: str | None
    category_parent_name: str | None
    writer_name: str | None
    opened_at: datetime | None
    date_mod: datetime
    date_solve: datetime | None
    date_close: datetime | None
    time_to_resolve: datetime | None
    pending_reason_name: str | None
    team: tuple[TeamMember, ...] = field(default_factory=tuple)
    followups: tuple[FollowupSnapshot, ...] = field(default_factory=tuple)
    solutions: tuple[SolutionSnapshot, ...] = field(default_factory=tuple)
    linked_items: tuple[LinkedItem, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ChangedTicket:
    """One result of the reconciliation search."""

    id: int
    date_mod: datetime


# --- lists for linking (Phase 15) ------------------------------------------------------


@dataclass(frozen=True)
class GlpiNamed:
    """A GLPI Location or Group: id and name (HTML entities decoded)."""

    id: int
    name: str


@dataclass(frozen=True)
class GlpiAsset:
    """A GLPI asset (NetworkEquipment, Computer, Printer, Peripheral) and its location."""

    itemtype: str
    id: int
    name: str
    location_id: int | None


@dataclass(frozen=True)
class GlpiTicketRef:
    """One ticket from the V1 ticket list: enough to link and queue it."""

    id: int
    external_id: str | None
    location_id: int | None
    type: int
    date_mod: datetime | None
    is_deleted: bool
