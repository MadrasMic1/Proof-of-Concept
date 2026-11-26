# app/agentic/store_context/queries.py
"""Read-only queries for one store. Every query filters on store_id.

Same tables as the app screens (app/queries), so the assistant and the screens
cannot disagree. Called inside build_snapshot's REPEATABLE READ transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.store_context.rows import (
    ActionRow,
    AlertRow,
    DeviceRow,
    EdgeRow,
    ElementRow,
    IncidentRow,
    NoticeRow,
    StoreRow,
    VisitRow,
)
from app.models import (
    AgentStoreDependency,
    Appointment,
    AssignmentGroup,
    Device,
    DeviceType,
    Incident,
    NetworkElement,
    NetworkElementCurrentStatus,
    NetworkElementType,
    Notification,
    NotificationTarget,
    Store,
    StoreAction,
    StoreAlert,
)
from app.models.values import ACTIVE_INCIDENT_STATES, ActionStatus, AppointmentStatus
from app.queries.notifications import live_filter

RESOLVED_STATES = (6, 7)  # resolved, closed
ACTIVE_ALERT = "active"
MAINTENANCE = "maintenance"
VISIT_GRACE = timedelta(hours=2)


async def database_now(session: AsyncSession) -> datetime:
    return await session.scalar(select(func.now()))


async def store_id_for_site(session: AsyncSession, site_code: str) -> uuid.UUID | None:
    return await session.scalar(select(Store.id).where(Store.site_code == site_code))


async def load_store(session: AsyncSession, store_id: uuid.UUID) -> StoreRow | None:
    row = (
        await session.execute(
            select(
                Store.id,
                Store.site_code,
                Store.name,
                Store.timezone,
                Store.business_hours,
                Store.status,
            ).where(Store.id == store_id)
        )
    ).one_or_none()
    if row is None:
        return None
    return StoreRow(
        id=row.id,
        site_code=row.site_code,
        name=row.name,
        timezone=row.timezone,
        business_hours=row.business_hours or {},
        status=row.status,
    )


async def load_elements(session: AsyncSession, store_id: uuid.UUID) -> list[ElementRow]:
    rows = (
        await session.execute(
            select(
                NetworkElement.id,
                NetworkElement.name,
                NetworkElement.element_type_code,
                NetworkElement.role,
                NetworkElement.location_in_store,
                NetworkElement.uplink_element_id,
                NetworkElementType.business_criticality,
                NetworkElementCurrentStatus.status,
                NetworkElementCurrentStatus.status_since,
                NetworkElementCurrentStatus.last_event_at,
            )
            .join(NetworkElementType, NetworkElementType.code == NetworkElement.element_type_code)
            .outerjoin(
                NetworkElementCurrentStatus,
                NetworkElementCurrentStatus.network_element_id == NetworkElement.id,
            )
            .where(NetworkElement.store_id == store_id, NetworkElement.is_active)
            .order_by(NetworkElementType.sort_order, NetworkElement.name)
        )
    ).all()
    return [
        ElementRow(
            id=row.id,
            name=row.name,
            type_code=row.element_type_code,
            role=row.role,
            location=row.location_in_store,
            uplink_element_id=row.uplink_element_id,
            criticality=row.business_criticality,
            status=row.status,
            status_since=row.status_since,
            last_event_at=row.last_event_at,
        )
        for row in rows
    ]


async def load_devices(session: AsyncSession, store_id: uuid.UUID) -> list[DeviceRow]:
    rows = (
        await session.execute(
            select(
                Device.id,
                Device.name,
                Device.device_type_code,
                Device.location_in_store,
                Device.connection_type,
                Device.connected_network_element_id,
                DeviceType.business_criticality,
                Device.status,
                Device.status_since,
                Device.status_reason,
                Device.last_heartbeat_at,
            )
            .join(DeviceType, DeviceType.code == Device.device_type_code)
            .where(Device.store_id == store_id, Device.is_active)
            .order_by(DeviceType.sort_order, Device.name)
        )
    ).all()
    return [
        DeviceRow(
            id=row.id,
            name=row.name,
            type_code=row.device_type_code,
            location=row.location_in_store,
            connection_type=row.connection_type,
            connected_element_id=row.connected_network_element_id,
            criticality=row.business_criticality,
            status=row.status,
            status_since=row.status_since,
            status_reason=row.status_reason,
            last_heartbeat_at=row.last_heartbeat_at,
        )
        for row in rows
    ]


async def load_edges(session: AsyncSession, store_id: uuid.UUID) -> list[EdgeRow]:
    rows = (
        await session.scalars(
            select(AgentStoreDependency).where(AgentStoreDependency.store_id == store_id)
        )
    ).all()
    return [
        EdgeRow(
            from_kind=row.from_kind,
            from_device_id=row.from_device_id,
            from_element_id=row.from_element_id,
            to_kind=row.to_kind,
            to_device_id=row.to_device_id,
            to_element_id=row.to_element_id,
            to_service=row.to_service,
            relation=row.relation,
            redundancy_group=row.redundancy_group,
            note=row.note,
        )
        for row in rows
    ]


async def load_incidents(
    session: AsyncSession, store_id: uuid.UUID, now: datetime, resolved_hours: int
) -> list[IncidentRow]:
    """Active incidents, plus those resolved or closed in the last resolved_hours."""
    rows = (
        await session.execute(
            select(
                Incident.id,
                Incident.number,
                Incident.short_description,
                Incident.state,
                Incident.priority,
                Incident.category,
                AssignmentGroup.name.label("group_name"),
                Incident.opened_at,
                Incident.u_resolution_eta,
                Incident.u_eta_note,
                Incident.cmdb_ci_device_id,
                Incident.cmdb_ci_network_element_id,
                Incident.resolved_at,
            )
            .outerjoin(AssignmentGroup, AssignmentGroup.id == Incident.assignment_group_id)
            .where(
                Incident.store_id == store_id,
                or_(
                    Incident.state.in_(ACTIVE_INCIDENT_STATES),
                    and_(
                        Incident.state.in_(RESOLVED_STATES),
                        Incident.resolved_at >= now - timedelta(hours=resolved_hours),
                    ),
                ),
            )
            .order_by(Incident.opened_at.desc())
        )
    ).all()
    return [
        IncidentRow(
            id=row.id,
            number=row.number,
            short_description=row.short_description,
            state=row.state,
            priority=row.priority,
            category=row.category,
            assignment_group=row.group_name,
            opened_at=row.opened_at,
            eta=row.u_resolution_eta,
            eta_note=row.u_eta_note,
            device_id=row.cmdb_ci_device_id,
            element_id=row.cmdb_ci_network_element_id,
            resolved_at=row.resolved_at,
        )
        for row in rows
    ]


async def load_visits(
    session: AsyncSession, store_id: uuid.UUID, now: datetime, lookahead_hours: int
) -> list[VisitRow]:
    rows = (
        await session.execute(
            select(
                Appointment.title,
                Appointment.appointment_type,
                Appointment.scheduled_start,
                Appointment.scheduled_end,
                Appointment.status,
                Incident.number,
            )
            .join(Incident, Incident.id == Appointment.incident_id)
            .where(
                Appointment.store_id == store_id,
                Appointment.status.in_(
                    (AppointmentStatus.SCHEDULED, AppointmentStatus.IN_PROGRESS)
                ),
                Appointment.scheduled_start >= now - VISIT_GRACE,
                Appointment.scheduled_start <= now + timedelta(hours=lookahead_hours),
            )
            .order_by(Appointment.scheduled_start)
        )
    ).all()
    return [
        VisitRow(
            title=row.title,
            appointment_type=row.appointment_type,
            scheduled_start=row.scheduled_start,
            scheduled_end=row.scheduled_end,
            status=row.status,
            incident_number=row.number,
        )
        for row in rows
    ]


async def load_alerts(session: AsyncSession, store_id: uuid.UUID) -> list[AlertRow]:
    rows = (
        await session.execute(
            select(StoreAlert.title, StoreAlert.severity, StoreAlert.created_at)
            .where(StoreAlert.store_id == store_id, StoreAlert.status == ACTIVE_ALERT)
            .order_by(StoreAlert.created_at.desc())
        )
    ).all()
    return [AlertRow(title=r.title, severity=r.severity, created_at=r.created_at) for r in rows]


async def load_actions(session: AsyncSession, store_id: uuid.UUID) -> list[ActionRow]:
    rows = (
        await session.execute(
            select(
                StoreAction.title, StoreAction.severity, StoreAction.due_by, StoreAction.created_at
            )
            .where(StoreAction.store_id == store_id, StoreAction.status == ActionStatus.OPEN)
            .order_by(StoreAction.created_at)
        )
    ).all()
    return [
        ActionRow(title=r.title, severity=r.severity, due_by=r.due_by, created_at=r.created_at)
        for r in rows
    ]


async def load_maintenance_notices(
    session: AsyncSession, store_id: uuid.UUID, now: datetime
) -> list[NoticeRow]:
    rows = (
        await session.execute(
            select(
                Notification.title,
                Notification.category,
                Notification.publish_at,
                Notification.expires_at,
            )
            .select_from(NotificationTarget)
            .join(Notification, Notification.id == NotificationTarget.notification_id)
            .where(
                NotificationTarget.store_id == store_id,
                Notification.category == MAINTENANCE,
                live_filter(now),
            )
            .order_by(Notification.publish_at.desc())
        )
    ).all()
    return [
        NoticeRow(
            title=r.title, category=r.category, publish_at=r.publish_at, expires_at=r.expires_at
        )
        for r in rows
    ]
