# app/agentic/common/types.py
"""Types shared by the store graph, the MCP tools and the agents.

Status values follow the database: devices are online / degraded / offline /
unknown, network elements online / offline / unknown; the graph adds
maintenance (a planned window covers the node).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class NodeKind(StrEnum):
    DEVICE = "device"
    NETWORK_ELEMENT = "network_element"
    SERVICE = "service"
    STORE = "store"


class NodeStatus(StrEnum):
    ONLINE = "online"
    DEGRADED = "degraded"
    OFFLINE = "offline"
    UNKNOWN = "unknown"
    MAINTENANCE = "maintenance"


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class NodeRef(Frozen):
    """One node of the store graph, with both an id and words a person understands."""

    kind: NodeKind
    id: str  # uuid text for devices / network elements, the code for services
    name: str  # "PRN-02"
    element_type: str  # catalogue type, e.g. "receipt_printer"
    label: str  # "Receipt printer, Till 2"

    @model_validator(mode="after")
    def _id_matches_kind(self) -> NodeRef:
        if self.kind in (NodeKind.DEVICE, NodeKind.NETWORK_ELEMENT, NodeKind.STORE):
            uuid.UUID(self.id)
        return self


class PathStep(Frozen):
    node: NodeRef
    status: NodeStatus
    relation: str | None = None  # how this node depends on the next one
    last_report_at: datetime | None = None


class EvidenceRef(Frozen):
    source: str  # table or "graph"
    record_ids: list[str] = Field(default_factory=list)
    as_of: datetime


class Finding(Frozen):
    finding_id: str
    kind: Literal["root_cause", "suspect", "stale"]
    severity: Literal["critical", "major", "minor", "info"]
    root_node: NodeRef
    status: NodeStatus
    fault_code: str | None = None
    impacted_nodes: list[NodeRef] = Field(default_factory=list)
    evidence_path: list[PathStep] = Field(default_factory=list)
    impact_text: str
    staff_can_fix: bool
    kb_tags: list[str] = Field(default_factory=list)
    covered_by_incident: str | None = None  # incident number, e.g. INC0001234
    in_maintenance: bool = False
    evidence: list[EvidenceRef] = Field(default_factory=list)


class IncidentSummary(Frozen):
    """What agents see of an incident (shaped like the incidents table)."""

    number: str
    short_description: str
    state: int  # 1 new, 2 in progress, 3 on hold, 6 resolved, 7 closed, 8 canceled
    state_label: str
    priority: int = Field(ge=1, le=5)  # incidents.priority = impact + urgency - 1
    category: str
    assignment_group: str | None = None
    opened_at: datetime
    eta: datetime | None = None  # u_resolution_eta
    eta_note: str | None = None  # u_eta_note
    affected: NodeRef | None = None  # cmdb_ci_device_id or cmdb_ci_network_element_id
