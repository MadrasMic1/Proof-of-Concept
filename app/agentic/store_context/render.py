# app/agentic/store_context/render.py
"""The snapshot as compact text for the orchestrator's prompt.

Order: header, findings, network, devices, incidents, visits, store actions and
alerts, announcements, unknown status. Problems first; healthy items as counts.
Budget: STORE_CTX_SNAPSHOT_MAX_TOKENS, estimated as characters / 4. When over it,
trim in this order: announcements, visits, alerts/actions, healthy device counts,
minor findings (one at a time, "+N more minor findings"), device problem lines.
Critical and major findings are never removed.
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from app.agentic.common.types import Finding, NodeKind, NodeStatus
from app.agentic.store_context.assemble import NodeView, StoreSnapshot

CHARS_PER_TOKEN = 4
PROBLEM = (NodeStatus.OFFLINE, NodeStatus.DEGRADED, NodeStatus.UNKNOWN)


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def _local(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%H:%M") if value else "?"


def _finding_line(finding: Finding, snapshot: StoreSnapshot, tz: ZoneInfo) -> str:
    line = f"  {finding.finding_id} [{finding.severity}] {finding.status.value.upper()}"
    if finding.fault_code:
        line += f" {finding.fault_code}"
    line += f": {finding.impact_text}"
    if finding.impacted_nodes:
        names = ", ".join(n.name for n in finding.impacted_nodes[:6])
        more = len(finding.impacted_nodes) - 6
        line += f" Affected: {names}{f' +{more} more' if more > 0 else ''}."
    if finding.covered_by_incident:
        incident = next(
            (i for i in snapshot.incidents if i.number == finding.covered_by_incident), None
        )
        line += f" Covered by {finding.covered_by_incident}"
        if incident is not None:
            eta = _local(incident.eta, tz) if incident.eta else incident.eta_note
            line += f" ({incident.assignment_group or 'IT'}, {incident.state_label}"
            line += f", ETA {eta})" if eta else ")"
        line += "."
    elif finding.kind == "root_cause":
        line += " No incident yet."
    if finding.staff_can_fix:
        line += " Staff can fix."
    return line


def _network_line(nodes: list[NodeView], tz: ZoneInfo) -> str:
    elements = [v for v in nodes if v.node.kind == NodeKind.NETWORK_ELEMENT]
    if not elements:
        return "Network: no network elements recorded"
    parts: list[str] = []
    by_type: dict[str, list[NodeView]] = {}
    for view in elements:
        by_type.setdefault(view.node.element_type, []).append(view)
    for type_code, views in by_type.items():
        bad = [v for v in views if v.reported in PROBLEM]
        if type_code == "wan_link":
            parts += [
                f"{v.node.name} {v.reported.value.upper()}"
                + (f" since {_local(v.since, tz)}" if v.reported in PROBLEM else "")
                for v in views
            ]
            continue
        part = f"{type_code} {len(views) - len(bad)}/{len(views)} online"
        if bad:
            part += " (" + ", ".join(f"{v.node.name} {v.reported.value}" for v in bad) + ")"
        parts.append(part)
    return "Network: " + " | ".join(parts)


def _device_lines(nodes: list[NodeView], tz: ZoneInfo, max_lines: int) -> tuple[str, list[str]]:
    devices = [v for v in nodes if v.node.kind == NodeKind.DEVICE]
    totals = Counter(v.node.element_type for v in devices)
    healthy = Counter(v.node.element_type for v in devices if v.reported == NodeStatus.ONLINE)
    counts = "Devices: " + ", ".join(f"{t} {healthy[t]}/{n}" for t, n in totals.items())
    problems = [v for v in devices if v.reported in PROBLEM]
    lines = [
        f"  {v.node.label} {v.reported.value.upper()} since {_local(v.since, tz)}"
        + (f" - {v.reason}" if v.reason else "")
        for v in problems[:max_lines]
    ]
    if len(problems) > max_lines:
        lines.append(f"  +{len(problems) - max_lines} more devices with problems")
    return counts, lines


def render_snapshot(
    snapshot: StoreSnapshot,
    *,
    max_tokens: int,
    max_device_lines: int,
    max_incident_lines: int,
) -> str:
    store = snapshot.store
    tz = ZoneInfo(store.timezone)
    header = (
        f"Store {store.site_code} {store.name} | local {store.local_time.strftime('%a %H:%M')} "
        f"({'open' if store.open_now else 'closed'}) | data as of "
        f"{snapshot.as_of.astimezone(ZoneInfo('UTC')).strftime('%H:%M:%S')} UTC"
    )
    keep = [f for f in snapshot.findings if f.severity in ("critical", "major")]
    minor = [f for f in snapshot.findings if f.severity not in ("critical", "major")]
    device_counts, device_problems = _device_lines(snapshot.nodes, tz, max_device_lines)
    incidents = [
        f"  {i.number} {i.short_description} - {i.state_label}"
        + (f", {i.assignment_group}" if i.assignment_group else "")
        for i in snapshot.incidents[:max_incident_lines]
    ]
    if len(snapshot.incidents) > max_incident_lines:
        incidents.append(f"  +{len(snapshot.incidents) - max_incident_lines} more incidents")
    visits = [
        f"  {_local(v.starts_at, tz)} {v.title}"
        + (f" ({v.incident_number})" if v.incident_number else "")
        for v in snapshot.visits
    ]
    asks = [f"  Action: {a.title}" for a in snapshot.actions] + [
        f"  Alert [{a.severity}]: {a.title}" for a in snapshot.alerts
    ]
    notices = [f"  {n.title}" for n in snapshot.notices]
    sections = {
        "notices": notices,
        "visits": visits,
        "asks": asks,
        "device_counts": [device_counts],
    }
    dropped_minor = 0

    def build() -> str:
        lines = [header]
        shown = keep + minor
        lines.append("Findings:" if shown else "Findings: none - everything reports normally")
        lines += [_finding_line(f, snapshot, tz) for f in shown]
        if dropped_minor:
            lines.append(f"  +{dropped_minor} more minor findings")
        lines.append(_network_line(snapshot.nodes, tz))
        lines += sections["device_counts"]
        lines += device_problems
        count = len(snapshot.incidents)
        lines.append(f"Incidents ({count} active):" if incidents else "Incidents: none active")
        lines += incidents
        if sections["visits"]:
            lines += ["Visits:", *sections["visits"]]
        if sections["asks"]:
            lines += ["Waiting on the store:", *sections["asks"]]
        if sections["notices"]:
            lines += ["Announcements:", *sections["notices"]]
        for warning in snapshot.warnings:
            lines.append(f"Warning: {warning}")
        return "\n".join(lines)

    text = build()
    for name in ("notices", "visits", "asks", "device_counts"):
        if estimate_tokens(text) <= max_tokens:
            return text
        sections[name] = []
        text = build()
    while estimate_tokens(text) > max_tokens and minor:
        minor.pop()
        dropped_minor += 1
        text = build()
    while estimate_tokens(text) > max_tokens and device_problems:
        device_problems.pop()
        text = build()
    return text
