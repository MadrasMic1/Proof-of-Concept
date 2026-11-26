# app/agentic/store_context/rows.py
"""Plain rows read for one store (queries.py fills them; assemble.py uses them).

Kept free of SQLAlchemy so the graph and the snapshot can be tested without a database.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class StoreRow:
    id: uuid.UUID
    site_code: str
    name: str
    timezone: str
    business_hours: dict[str, Any]
    status: str


@dataclass(frozen=True)
class ElementRow:
    id: uuid.UUID
    name: str
    type_code: str
    role: str | None
    location: str | None
    uplink_element_id: uuid.UUID | None
    criticality: str
    status: str | None  # network_element_current_status.status; None = no row yet
    status_since: datetime | None
    last_event_at: datetime | None


@dataclass(frozen=True)
class DeviceRow:
    id: uuid.UUID
    name: str
    type_code: str
    location: str | None
    connection_type: str | None
    connected_element_id: uuid.UUID | None
    criticality: str
    status: str
    status_since: datetime | None
    status_reason: str | None
    last_heartbeat_at: datetime | None


@dataclass(frozen=True)
class EdgeRow:
    from_kind: str
    from_device_id: uuid.UUID | None
    from_element_id: uuid.UUID | None
    to_kind: str
    to_device_id: uuid.UUID | None
    to_element_id: uuid.UUID | None
    to_service: str | None
    relation: str
    redundancy_group: str | None
    note: str | None  # "preferred" marks the preferred member of a redundancy group


@dataclass(frozen=True)
class IncidentRow:
    id: uuid.UUID
    number: str
    short_description: str
    state: int
    priority: int
    category: str
    assignment_group: str | None
    opened_at: datetime
    eta: datetime | None
    eta_note: str | None
    device_id: uuid.UUID | None
    element_id: uuid.UUID | None
    resolved_at: datetime | None


@dataclass(frozen=True)
class VisitRow:
    title: str
    appointment_type: str
    scheduled_start: datetime
    scheduled_end: datetime | None
    status: str
    incident_number: str | None


@dataclass(frozen=True)
class AlertRow:
    title: str
    severity: str
    created_at: datetime


@dataclass(frozen=True)
class ActionRow:
    title: str
    severity: str
    due_by: datetime | None
    created_at: datetime


@dataclass(frozen=True)
class NoticeRow:
    title: str
    category: str
    publish_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class StoreState:
    """Everything read for one store in one REPEATABLE READ transaction."""

    store: StoreRow
    db_now: datetime
    elements: list[ElementRow] = field(default_factory=list)
    devices: list[DeviceRow] = field(default_factory=list)
    edges: list[EdgeRow] = field(default_factory=list)
    incidents: list[IncidentRow] = field(default_factory=list)  # active + recently resolved
    visits: list[VisitRow] = field(default_factory=list)
    alerts: list[AlertRow] = field(default_factory=list)
    actions: list[ActionRow] = field(default_factory=list)
    notices: list[NoticeRow] = field(default_factory=list)  # live maintenance notifications
