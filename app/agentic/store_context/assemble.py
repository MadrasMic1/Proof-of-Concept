# app/agentic/store_context/assemble.py
"""From the rows of one store to its graph, findings and snapshot. Pure.

build_store_graph()  nodes from devices and network elements (+ services the
                     explicit edges use), derived edges from the inventory columns,
                     explicit edges from agent_store_dependencies, and each service's
                     edges to its redundancy group (catalogue: via_group).
assemble_snapshot()  the typed StoreSnapshot the orchestrator and the tools use.

Stale rule (STORE_CTX_STALE_CHECK, off by default): a device that reports online or
degraded but has no heartbeat for STORE_CTX_STALE_AFTER_MINUTES becomes unknown.
Network elements are event-driven (Aruba reports changes, not heartbeats), so their
status never goes stale by age. Off by default because the POC seed fixes heartbeats
at seed time; with it off the assistant shows exactly what the app screens show.

Maintenance: notifications are store-wide announcements without the affected
element or the actual window, so they are listed, never used to mark nodes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import Field

from app.agentic.common.types import (
    Finding,
    Frozen,
    IncidentSummary,
    NodeKind,
    NodeRef,
    NodeStatus,
)
from app.agentic.config import AgenticSettings
from app.agentic.store_context.rows import DeviceRow, ElementRow, IncidentRow, StoreState
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.store_graph.engine import analyse
from app.agentic.store_graph.findings import IncidentLink, build_findings
from app.agentic.store_graph.model import GraphEdge, GraphError, GraphNode, StoreGraph, node_key
from app.core.timefmt import is_open
from app.models.values import ACTIVE_INCIDENT_STATES
from app.services.incident_rules import state_label

PREFERRED_NOTE = "preferred"


class StoreInfo(Frozen):
    id: str
    site_code: str
    name: str
    timezone: str
    local_time: datetime
    open_now: bool
    status: str


class NodeView(Frozen):
    node: NodeRef
    reported: NodeStatus  # what the inventory says (unknown when stale)
    effective: NodeStatus  # services: derived from their dependencies
    since: datetime | None = None
    reason: str | None = None
    location: str | None = None
    role: str | None = None
    stale: bool = False


class VisitView(Frozen):
    title: str
    appointment_type: str
    starts_at: datetime
    ends_at: datetime | None = None
    status: str
    incident_number: str | None = None


class AlertView(Frozen):
    title: str
    severity: str
    created_at: datetime


class ActionView(Frozen):
    title: str
    severity: str
    due_by: datetime | None = None
    created_at: datetime


class NoticeView(Frozen):
    title: str
    category: str
    publish_at: datetime
    expires_at: datetime | None = None


class StoreSnapshot(Frozen):
    store: StoreInfo
    as_of: datetime  # the database time of the read
    nodes: list[NodeView] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    incidents: list[IncidentSummary] = Field(default_factory=list)  # active
    recently_resolved: list[IncidentSummary] = Field(default_factory=list)
    visits: list[VisitView] = Field(default_factory=list)
    alerts: list[AlertView] = Field(default_factory=list)
    actions: list[ActionView] = Field(default_factory=list)
    notices: list[NoticeView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def node(self, node_id: str) -> NodeView | None:
        return next((view for view in self.nodes if view.node.id == node_id), None)


def _status(value: str | None) -> NodeStatus:
    try:
        return NodeStatus(value) if value else NodeStatus.UNKNOWN
    except ValueError:
        return NodeStatus.UNKNOWN


def _element_node(row: ElementRow, catalogue: Catalogue) -> GraphNode:
    entry = catalogue.entry(row.type_code)
    label = f"{row.role or entry.label} {row.name}"
    if row.location:
        label = f"{label} ({row.location})"
    return GraphNode(
        key=node_key(NodeKind.NETWORK_ELEMENT, str(row.id)),
        kind=NodeKind.NETWORK_ELEMENT,
        id=str(row.id),
        name=row.name,
        type_code=row.type_code,
        label=label,
        status=_status(row.status),
        status_since=row.status_since,
        role=row.role,
        location=row.location,
        criticality=row.criticality,
    )


def _device_node(
    row: DeviceRow, catalogue: Catalogue, now: datetime, stale_after: timedelta | None
) -> GraphNode:
    entry = catalogue.entry(row.type_code)
    label = row.name if entry.label.lower() in row.name.lower() else f"{entry.label} {row.name}"
    if row.location:
        label = f"{label} ({row.location})"
    status = _status(row.status)
    stale = False
    if stale_after is not None and status in (NodeStatus.ONLINE, NodeStatus.DEGRADED):
        heartbeat = row.last_heartbeat_at
        if heartbeat is None or now - heartbeat > stale_after:
            status, stale = NodeStatus.UNKNOWN, True
    return GraphNode(
        key=node_key(NodeKind.DEVICE, str(row.id)),
        kind=NodeKind.DEVICE,
        id=str(row.id),
        name=row.name,
        type_code=row.type_code,
        label=label,
        status=status,
        status_since=row.status_since if not stale else row.last_heartbeat_at,
        reason=row.status_reason,
        location=row.location,
        criticality=row.criticality,
        stale=stale,
    )


def build_store_graph(
    state: StoreState,
    catalogue: Catalogue,
    *,
    now: datetime,
    stale_after: timedelta | None,
) -> tuple[StoreGraph, list[str]]:
    """The store graph and warnings about edges that had to be skipped."""
    warnings: list[str] = []
    nodes: dict[str, GraphNode] = {}
    for element in state.elements:
        node = _element_node(element, catalogue)
        nodes[node.key] = node
    for device in state.devices:
        node = _device_node(device, catalogue, now, stale_after)
        nodes[node.key] = node

    edges: list[GraphEdge] = []
    for element in state.elements:
        if element.uplink_element_id is not None:
            target = node_key(NodeKind.NETWORK_ELEMENT, str(element.uplink_element_id))
            if target in nodes:
                source = node_key(NodeKind.NETWORK_ELEMENT, str(element.id))
                edges.append(GraphEdge(source, target, "connects_via", derived=True))
    for device in state.devices:
        if device.connected_element_id is not None:
            target = node_key(NodeKind.NETWORK_ELEMENT, str(device.connected_element_id))
            if target in nodes:
                source = node_key(NodeKind.DEVICE, str(device.id))
                edges.append(GraphEdge(source, target, "connects_via", derived=True))

    for row in state.edges:
        source_id = row.from_device_id or row.from_element_id
        source = node_key(row.from_kind, str(source_id))
        if row.to_kind == NodeKind.SERVICE:
            code = row.to_service or ""
            if code not in catalogue.services:
                warnings.append(f"edge to unknown service {code!r} skipped")
                continue
            target = node_key(NodeKind.SERVICE, code)
            if target not in nodes:
                service = catalogue.service(code)
                nodes[target] = GraphNode(
                    key=target,
                    kind=NodeKind.SERVICE,
                    id=code,
                    name=service.label,
                    type_code=code,
                    label=service.label,
                    status=None,
                )
        else:
            target_id = row.to_device_id or row.to_element_id
            target = node_key(row.to_kind, str(target_id))
        if source not in nodes or target not in nodes:
            warnings.append(f"edge {row.relation} to an inactive or unknown node skipped")
            continue
        edges.append(
            GraphEdge(
                source,
                target,
                row.relation,
                group=row.redundancy_group,
                preferred=row.note == PREFERRED_NOTE,
            )
        )

    for key, node in list(nodes.items()):
        if node.kind != NodeKind.SERVICE:
            continue
        group = catalogue.service(node.type_code).via_group
        members = [e for e in edges if e.group == group and e.source != key]
        if not members:
            warnings.append(f"service {node.type_code!r}: no redundancy group {group!r}")
            del nodes[key]
            edges = [e for e in edges if key not in (e.source, e.target)]
            continue
        seen: set[str] = set()
        for member in members:
            if member.target not in seen:
                seen.add(member.target)
                edges.append(GraphEdge(key, member.target, "uses_service", group, member.preferred))
    return StoreGraph(nodes, edges), warnings


def _incident_summary(row: IncidentRow, nodes: dict[str, GraphNode]) -> IncidentSummary:
    affected = None
    if row.device_id is not None:
        node = nodes.get(node_key(NodeKind.DEVICE, str(row.device_id)))
        affected = node.ref() if node else None
    elif row.element_id is not None:
        node = nodes.get(node_key(NodeKind.NETWORK_ELEMENT, str(row.element_id)))
        affected = node.ref() if node else None
    return IncidentSummary(
        number=row.number,
        short_description=row.short_description,
        state=row.state,
        state_label=state_label(row.state),
        priority=row.priority,
        category=row.category,
        assignment_group=row.assignment_group,
        opened_at=row.opened_at,
        eta=row.eta,
        eta_note=row.eta_note,
        affected=affected,
    )


def assemble_snapshot(
    state: StoreState,
    catalogue: Catalogue,
    *,
    now: datetime,
    settings: AgenticSettings,
) -> StoreSnapshot:
    store = state.store
    tz = ZoneInfo(store.timezone)
    stale_after = (
        timedelta(minutes=settings.store_ctx_stale_after_minutes)
        if settings.store_ctx_stale_check
        else None
    )
    active_rows = [row for row in state.incidents if row.state in ACTIVE_INCIDENT_STATES]
    resolved_rows = [row for row in state.incidents if row.state not in ACTIVE_INCIDENT_STATES]

    warnings: list[str] = []
    findings: list[Finding] = []
    nodes: dict[str, GraphNode] = {}
    effective: dict[str, NodeStatus] = {}
    try:
        graph, warnings = build_store_graph(state, catalogue, now=now, stale_after=stale_after)
        nodes = graph.nodes
        analysis = analyse(
            graph,
            max_depth=settings.store_graph_max_depth,
            common_cause_min=settings.store_graph_common_cause_min,
            common_cause_window=timedelta(minutes=settings.store_graph_common_cause_window_minutes),
        )
        effective = analysis.effective
        links = [
            IncidentLink(
                number=row.number,
                device_id=str(row.device_id) if row.device_id else None,
                element_id=str(row.element_id) if row.element_id else None,
                opened_at=row.opened_at,
            )
            for row in active_rows
        ]
        findings = build_findings(
            graph,
            analysis,
            catalogue,
            incidents=links,
            as_of=state.db_now,
            impact_high_min_nodes=settings.incident_impact_high_min_nodes,
        )
    except GraphError as exc:
        warnings.append(f"store graph not usable: {exc}")

    views = [
        NodeView(
            node=node.ref(),
            reported=node.status or effective.get(key, NodeStatus.UNKNOWN),
            effective=effective.get(key, node.status or NodeStatus.UNKNOWN),
            since=node.status_since,
            reason=node.reason,
            location=node.location,
            role=node.role,
            stale=node.stale,
        )
        for key, node in nodes.items()
    ]
    return StoreSnapshot(
        store=StoreInfo(
            id=str(store.id),
            site_code=store.site_code,
            name=store.name,
            timezone=store.timezone,
            local_time=now.astimezone(tz),
            open_now=is_open(store.business_hours or {}, tz, now),
            status=store.status,
        ),
        as_of=state.db_now,
        nodes=views,
        findings=findings,
        incidents=[_incident_summary(row, nodes) for row in active_rows],
        recently_resolved=[_incident_summary(row, nodes) for row in resolved_rows],
        visits=[
            VisitView(
                title=v.title,
                appointment_type=v.appointment_type,
                starts_at=v.scheduled_start,
                ends_at=v.scheduled_end,
                status=v.status,
                incident_number=v.incident_number,
            )
            for v in state.visits
        ],
        alerts=[
            AlertView(title=a.title, severity=a.severity, created_at=a.created_at)
            for a in state.alerts
        ],
        actions=[
            ActionView(title=a.title, severity=a.severity, due_by=a.due_by, created_at=a.created_at)
            for a in state.actions
        ],
        notices=[
            NoticeView(
                title=n.title, category=n.category, publish_at=n.publish_at, expires_at=n.expires_at
            )
            for n in state.notices
        ],
        warnings=warnings,
    )
