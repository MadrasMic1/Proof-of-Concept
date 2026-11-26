# tests/agentic/seed_scenario.py
"""Build a store's rows straight from the seed files, without a database.

Reads seed/data (stores, master, inventory_template, store_overrides,
event_scenarios, incidents) and agent_dependencies.yaml the same way the seed
does, so the pure scenario tests describe exactly what the seeded database holds:
an element is offline when its latest outage started before the anchor and has
not ended; devices take their override status; incidents keep their state.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from app.agentic.store_context.rows import (
    DeviceRow,
    EdgeRow,
    ElementRow,
    IncidentRow,
    StoreRow,
    StoreState,
)
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.store_graph.dependencies import load_dependency_template

ROOT = Path(__file__).resolve().parents[2]
SEED = ROOT / "seed" / "data"
_OFFSET = re.compile(r"^([+-])?(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?$")


def offset(value: str) -> timedelta:
    match = _OFFSET.fullmatch(value.strip())
    if not match or not any(match.groups()[1:]):
        raise ValueError(f"bad offset {value!r}")
    sign, days, hours, minutes = match.groups()
    delta = timedelta(days=int(days or 0), hours=int(hours or 0), minutes=int(minutes or 0))
    return -delta if sign == "-" else delta


def _yaml(name: str) -> dict[str, Any]:
    return yaml.safe_load((SEED / name).read_text(encoding="utf-8"))


def _id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(uuid.NAMESPACE_URL, "ric-test/" + "/".join(parts))


def seed_state(site: str, anchor: datetime, catalogue: Catalogue) -> StoreState:
    stores = {s["site_code"]: s for s in _yaml("stores.yaml")["stores"]}
    store = stores[site]
    master = _yaml("master.yaml")
    criticality = {
        t["code"]: t["business_criticality"]
        for t in [*master["network_element_types"], *master["device_types"]]
    }
    template = _yaml("inventory_template.yaml")
    scenarios = (_yaml("event_scenarios.yaml").get("stores") or {}).get(site) or {}
    overrides = ((_yaml("store_overrides.yaml").get("stores") or {}).get(site) or {}).get(
        "devices"
    ) or {}

    keys: dict[str, uuid.UUID] = {}
    names: dict[str, str] = {}
    for element in template["network_elements"]:
        name = element["name"].format(site=site)
        keys[element["key"]] = _id(site, name)
        names[element["key"]] = name

    def element_status(key: str) -> tuple[str, datetime]:
        status, since = "online", anchor + offset(_yaml("event_scenarios.yaml")["baseline_at"])
        outages = sorted(
            (o for o in scenarios.get("outages", []) if o["element"] == key),
            key=lambda o: offset(o["down_at"]),
        )
        for outage in outages:
            down = anchor + offset(outage["down_at"])
            up = anchor + offset(outage["up_at"]) if outage.get("up_at") else None
            if down <= anchor:
                status, since = "offline", down
            if up is not None and up <= anchor:
                status, since = "online", up
        return status, since

    elements: list[ElementRow] = []
    for element in template["network_elements"]:
        status, since = element_status(element["key"])
        elements.append(
            ElementRow(
                id=keys[element["key"]],
                name=names[element["key"]],
                type_code=element["type"],
                role=element.get("role"),
                location=element.get("location"),
                uplink_element_id=keys.get(element.get("uplink") or ""),
                criticality=criticality[element["type"]],
                status=status,
                status_since=since,
                last_event_at=since,
            )
        )

    devices: list[DeviceRow] = []
    for spec in template["devices"]:
        targets = spec["connected_to"]
        targets = targets if isinstance(targets, list) else [targets]
        for n in range(1, spec["count"] + 1):
            name = spec["name"].format(n=n)
            override = overrides.get(name)
            status = override["status"] if override else "online"
            since = anchor + offset(override["since"]) if override else anchor - timedelta(days=3)
            devices.append(
                DeviceRow(
                    id=_id(site, name),
                    name=name,
                    type_code=spec["type"],
                    location=spec["location"].format(n=n) if spec.get("location") else None,
                    connection_type=spec.get("connection"),
                    connected_element_id=keys[targets[(n - 1) % len(targets)]],
                    criticality=criticality[spec["type"]],
                    status=status,
                    status_since=since,
                    status_reason=override.get("reason") if override else None,
                    last_heartbeat_at=anchor - timedelta(minutes=1),
                )
            )

    dependencies = load_dependency_template(
        SEED / "agent_dependencies.yaml", SEED / "inventory_template.yaml", catalogue
    )
    edges = [
        EdgeRow(
            from_kind="network_element",
            from_device_id=None,
            from_element_id=keys[spec.from_key],
            to_kind="network_element",
            to_device_id=None,
            to_element_id=keys[spec.to_key],
            to_service=None,
            relation=spec.relation,
            redundancy_group=spec.redundancy_group,
            note="preferred" if spec.preferred else None,
        )
        for spec in dependencies.element_edges
    ]
    for service in dependencies.device_services:
        for device in devices:
            if device.type_code == service.device_type:
                edges.append(
                    EdgeRow(
                        from_kind="device",
                        from_device_id=device.id,
                        from_element_id=None,
                        to_kind="service",
                        to_device_id=None,
                        to_element_id=None,
                        to_service=service.service,
                        relation="uses_service",
                        redundancy_group=None,
                        note=None,
                    )
                )

    by_name = {d.name: d.id for d in devices} | {e.name: e.id for e in elements}
    incidents = [
        IncidentRow(
            id=_id(site, item["number"]),
            number=item["number"],
            short_description=item["short_description"],
            state=item["state"],
            priority=item["impact"] + item["urgency"] - 1,
            category=item["category"],
            assignment_group=item.get("assignment_group"),
            opened_at=anchor + offset(item["opened_at"]),
            eta=None,
            eta_note=item.get("eta_note"),
            device_id=by_name.get(item["ci_device"]) if item.get("ci_device") else None,
            element_id=by_name.get(item["ci_element"]) if item.get("ci_element") else None,
            resolved_at=anchor + offset(item["resolved_at"]) if item.get("resolved_at") else None,
        )
        for item in _yaml("incidents.yaml")["incidents"]
        if item["site"] == site
    ]
    return StoreState(
        store=StoreRow(
            id=_id(site),
            site_code=site,
            name=store["name"],
            timezone=store["timezone"],
            business_hours=store.get("business_hours") or {},
            status="open",
        ),
        db_now=anchor,
        elements=elements,
        devices=devices,
        edges=edges,
        incidents=incidents,
    )


def all_sites() -> list[str]:
    return [s["site_code"] for s in _yaml("stores.yaml")["stores"]]
