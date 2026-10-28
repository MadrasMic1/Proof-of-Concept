# app/models/itsm.py
"""Group 3: ITSM. incident_journal is also the worker queue.

Every appointment belongs to an incident (incident_id is NOT NULL since revision 0002).
GLPI sync (revision 0004): glpi_* ids link incidents and journal entries to GLPI;
itsm_inbox is the sync work queue and itsm_sync_state the scheduled sync jobs.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Computed,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Integer,
    SmallInteger,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAt, JsonObject, ServiceNowSysId, UpdatedAt, UUIDPk


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("id", "store_id"),
        ForeignKeyConstraint(
            ["cmdb_ci_network_element_id", "store_id"],
            ["network_elements.id", "network_elements.store_id"],
        ),
        ForeignKeyConstraint(
            ["cmdb_ci_device_id", "store_id"],
            ["devices.id", "devices.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    number: Mapped[str] = mapped_column(unique=True)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    short_description: Mapped[str]
    description: Mapped[str | None]
    u_symptom: Mapped[str | None]
    business_service: Mapped[str | None]
    category: Mapped[str]
    subcategory: Mapped[str | None]
    cmdb_ci_network_element_id: Mapped[uuid.UUID | None]
    cmdb_ci_device_id: Mapped[uuid.UUID | None]
    contact_type: Mapped[str]
    caller: Mapped[str | None]
    opened_by: Mapped[str | None]
    impact: Mapped[int] = mapped_column(SmallInteger)
    urgency: Mapped[int] = mapped_column(SmallInteger)
    priority: Mapped[int | None] = mapped_column(
        SmallInteger, Computed("impact + urgency - 1", persisted=True)
    )
    state: Mapped[int] = mapped_column(SmallInteger, server_default=text("1"))
    hold_reason: Mapped[str | None]
    assignment_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment_groups.id")
    )
    assigned_to: Mapped[str | None]
    u_resolution_eta: Mapped[datetime | None]
    u_eta_note: Mapped[str | None]
    opened_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    close_code: Mapped[str | None]
    close_notes: Mapped[str | None]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    glpi_ticket_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    itsm_updated_at: Mapped[datetime | None]
    last_synced_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class IncidentJournal(Base):
    __tablename__ = "incident_journal"

    id: Mapped[UUIDPk]
    incident_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("incidents.id"))
    element: Mapped[str]
    value: Mapped[str]
    author_type: Mapped[str]
    created_by: Mapped[str]
    source: Mapped[str]
    process_status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    processed_at: Mapped[datetime | None]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    glpi_followup_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    created_at: Mapped[CreatedAt]


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        ForeignKeyConstraint(
            ["incident_id", "store_id"],
            ["incidents.id", "incidents.store_id"],
        ),
    )

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    incident_id: Mapped[uuid.UUID]
    appointment_type: Mapped[str]
    title: Mapped[str]
    purpose: Mapped[str]
    details: Mapped[str | None]
    scheduled_start: Mapped[datetime]
    scheduled_end: Mapped[datetime | None]
    status: Mapped[str] = mapped_column(server_default=text("'scheduled'"))
    status_note: Mapped[str | None]
    vendor_name: Mapped[str | None]
    technician_name: Mapped[str | None]
    vendor_reference: Mapped[str | None]
    created_by: Mapped[str]
    source: Mapped[str]
    servicenow_sys_id: Mapped[ServiceNowSysId]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class ItsmInbox(Base):
    __tablename__ = "itsm_inbox"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    glpi_ticket_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str]
    events: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    merged_count: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    available_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    processed_at: Mapped[datetime | None]
    updated_at: Mapped[UpdatedAt]


class ItsmSyncState(Base):
    __tablename__ = "itsm_sync_state"

    job_name: Mapped[str] = mapped_column(primary_key=True)
    interval_seconds: Mapped[int] = mapped_column(Integer)
    next_run_at: Mapped[datetime]
    watermark: Mapped[datetime | None]
    last_started_at: Mapped[datetime | None]
    last_success_at: Mapped[datetime | None]
    consecutive_failures: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_error: Mapped[str | None]
    details: Mapped[JsonObject]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]
