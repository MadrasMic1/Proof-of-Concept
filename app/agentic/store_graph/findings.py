# app/agentic/store_graph/findings.py
"""Turn the engine's analysis into Findings: catalogue text, fault classification,
severity, incident overlay, evidence, stable ids and order. Pure.

Severity:
    critical  every member of a redundancy group is down, or the root explains at
              least INCIDENT_IMPACT_HIGH_MIN_NODES failing nodes
    major     failover; or a high-criticality type; or the root explains other nodes
    minor     reduced resilience (a backup member down), or anything else
    info      status unknown (stale)
Order: severity, then findings no incident covers yet, then kind, then name.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.agentic.common.types import (
    EvidenceRef,
    Finding,
    NodeKind,
    NodeStatus,
    PathStep,
)
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.store_graph.engine import Analysis, RootCause, Suspect
from app.agentic.store_graph.model import GraphNode, StoreGraph

SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "info": 3}
KIND_RANK = {"root_cause": 0, "suspect": 1, "stale": 2}
MAX_PATH_STEPS = 12


@dataclass(frozen=True)
class IncidentLink:
    """An active incident and its main CI (one of the two ids)."""

    number: str
    device_id: str | None
    element_id: str | None
    opened_at: datetime


class _SafeDict(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def finding_id(kind: str, key: str) -> str:
    return "F-" + hashlib.sha1(f"{kind}|{key}".encode()).hexdigest()[:8]


def _text(template: str, node: GraphNode, graph: StoreGraph, remaining: Sequence[str]) -> str:
    values = _SafeDict(
        name=node.name,
        label=node.label,
        location=node.location or "store",
        role=node.role or "",
        remaining=" and ".join(graph.nodes[k].label for k in remaining) or "the other link",
    )
    return " ".join(template.format_map(values).split())


def _step(node: GraphNode, status: NodeStatus) -> PathStep:
    return PathStep(node=node.ref(), status=status, last_report_at=node.status_since)


def _covering(
    keys: Sequence[str], graph: StoreGraph, incidents: Sequence[IncidentLink]
) -> str | None:
    """Newest active incident whose main CI is the first of keys that has one."""
    newest_first = sorted(incidents, key=lambda i: i.opened_at, reverse=True)
    for key in keys:
        node = graph.nodes[key]
        for incident in newest_first:
            if node.kind == NodeKind.DEVICE and incident.device_id == node.id:
                return incident.number
            if node.kind == NodeKind.NETWORK_ELEMENT and incident.element_id == node.id:
                return incident.number
    return None


def _evidence(node: GraphNode, covered: str | None, as_of: datetime) -> list[EvidenceRef]:
    source = "devices" if node.kind == NodeKind.DEVICE else "network_element_current_status"
    evidence = [EvidenceRef(source=source, record_ids=[node.id], as_of=as_of)]
    if covered:
        evidence.append(EvidenceRef(source="incidents", record_ids=[covered], as_of=as_of))
    return evidence


def _severity(root: RootCause, node: GraphNode, failing_impacted: int, high_min: int) -> str:
    if root.mode == "group_down" or failing_impacted >= high_min:
        return "critical"
    if root.mode == "reduced":
        return "minor"
    if root.mode == "failover" or node.criticality == "high" or failing_impacted > 0:
        return "major"
    return "minor"


def _root_finding(
    root: RootCause,
    graph: StoreGraph,
    analysis: Analysis,
    catalogue: Catalogue,
    incidents: Sequence[IncidentLink],
    as_of: datetime,
    high_min: int,
) -> Finding:
    node = graph.nodes[root.key]
    entry = catalogue.entry(node.type_code)
    templates = {
        "down": entry.impact_when_down,
        "degraded": entry.impact_when_degraded,
        "failover": entry.impact_when_failover or entry.impact_when_down,
        "reduced": entry.impact_when_reduced or entry.impact_when_down,
        "group_down": entry.impact_when_group_down or entry.impact_when_down,
    }
    impact = _text(templates[root.mode], node, graph, root.remaining)
    services = [k for k in root.impacted if graph.nodes[k].kind == NodeKind.SERVICE]
    for key in services:
        service = catalogue.service(graph.nodes[key].type_code)
        status = analysis.effective[key]
        extra = None
        if status == NodeStatus.OFFLINE:
            extra = service.impact_when_down
        elif status == NodeStatus.DEGRADED and root.mode != "failover":
            extra = service.impact_when_degraded
        if extra:
            impact = f"{impact} {extra}"
    classification = catalogue.classify(node.type_code, node.reason)
    failing = [k for k in root.impacted if graph.nodes[k].kind != NodeKind.SERVICE]
    covered = _covering([root.key, *root.group_members, *failing], graph, incidents)
    path = [_step(node, analysis.effective[root.key])]
    path += [_step(graph.nodes[k], analysis.effective[k]) for k in root.impacted]
    id_kind = "group_down" if root.mode == "group_down" else "root_cause"
    return Finding(
        finding_id=finding_id(id_kind, root.key),
        kind="root_cause",
        severity=_severity(root, node, len(failing), high_min),
        root_node=node.ref(),
        status=analysis.effective[root.key],
        fault_code=classification.fault_code,
        impacted_nodes=[graph.nodes[k].ref() for k in root.impacted],
        evidence_path=path[:MAX_PATH_STEPS],
        impact_text=impact,
        staff_can_fix=classification.staff_can_fix and root.mode in ("down", "degraded"),
        kb_tags=list(classification.kb_tags),
        covered_by_incident=covered,
        evidence=_evidence(node, covered, as_of),
    )


def _suspect_finding(
    suspect: Suspect, graph: StoreGraph, analysis: Analysis, as_of: datetime
) -> Finding:
    node = graph.nodes[suspect.key]
    impact = (
        f"{len(suspect.failing)} items that need {node.label} went offline together; "
        f"{node.label} may be failing even though it still reports {analysis.effective[node.key]}."
    )
    return Finding(
        finding_id=finding_id("suspect", suspect.key),
        kind="suspect",
        severity="major" if node.criticality == "high" else "minor",
        root_node=node.ref(),
        status=analysis.effective[suspect.key],
        impacted_nodes=[graph.nodes[k].ref() for k in suspect.failing],
        evidence_path=[_step(node, analysis.effective[node.key])]
        + [_step(graph.nodes[k], analysis.effective[k]) for k in suspect.failing][
            : MAX_PATH_STEPS - 1
        ],
        impact_text=impact,
        staff_can_fix=False,
        evidence=[
            EvidenceRef(
                source="graph",
                record_ids=[node.id, *(graph.nodes[k].id for k in suspect.failing)],
                as_of=as_of,
            )
        ],
    )


def _stale_finding(key: str, graph: StoreGraph, as_of: datetime) -> Finding:
    node = graph.nodes[key]
    since = node.status_since.strftime("%Y-%m-%d %H:%M UTC") if node.status_since else "never"
    return Finding(
        finding_id=finding_id("stale", key),
        kind="stale",
        severity="info",
        root_node=node.ref(),
        status=NodeStatus.UNKNOWN,
        evidence_path=[_step(node, NodeStatus.UNKNOWN)],
        impact_text=f"Status of {node.label} is unknown (last report: {since}).",
        staff_can_fix=False,
        evidence=_evidence(node, None, as_of),
    )


def build_findings(
    graph: StoreGraph,
    analysis: Analysis,
    catalogue: Catalogue,
    *,
    incidents: Sequence[IncidentLink],
    as_of: datetime,
    impact_high_min_nodes: int,
) -> list[Finding]:
    findings = [
        _root_finding(root, graph, analysis, catalogue, incidents, as_of, impact_high_min_nodes)
        for root in analysis.roots
    ]
    findings += [_suspect_finding(s, graph, analysis, as_of) for s in analysis.suspects]
    findings += [_stale_finding(key, graph, as_of) for key in analysis.unknown]
    return sorted(
        findings,
        key=lambda f: (
            SEVERITY_RANK[f.severity],
            f.covered_by_incident is not None,
            KIND_RANK[f.kind],
            f.root_node.name,
        ),
    )
