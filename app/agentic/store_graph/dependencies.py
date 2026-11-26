# app/agentic/store_graph/dependencies.py
"""The explicit-edge template (seed/data/agent_dependencies.yaml). Pure.

Edges reference the element keys of seed/data/inventory_template.yaml, so the two
files stay in sync: a key that the inventory template does not define is an error.
Element names come from the template's name patterns, e.g. "WAN{site}-PRI".
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.agentic.store_graph.catalogue import Catalogue

RELATIONS = ("connects_via", "powered_by", "uses_service")
SLUG = re.compile(r"^[a-z][a-z0-9_]*$")


class DependencyTemplateError(ValueError):
    pass


@dataclass(frozen=True)
class ElementEdgeSpec:
    from_key: str
    to_key: str
    relation: str
    redundancy_group: str | None
    preferred: bool


@dataclass(frozen=True)
class DeviceServiceSpec:
    device_type: str
    service: str


@dataclass(frozen=True)
class DependencyTemplate:
    element_edges: tuple[ElementEdgeSpec, ...]
    device_services: tuple[DeviceServiceSpec, ...]
    element_names: dict[str, str]  # inventory key -> name pattern ("WAN{site}-PRI")

    def element_name(self, key: str, site_code: str) -> str:
        return self.element_names[key].format(site=site_code)


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise DependencyTemplateError(f"file not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise DependencyTemplateError(f"{path}: expected a mapping")
    return data


def load_dependency_template(
    dependencies_path: Path, inventory_template_path: Path, catalogue: Catalogue
) -> DependencyTemplate:
    inventory = _load(inventory_template_path)
    names = {str(e["key"]): str(e["name"]) for e in inventory.get("network_elements", [])}
    types = {str(e["key"]): str(e["type"]) for e in inventory.get("network_elements", [])}
    device_types = {str(d["type"]) for d in inventory.get("devices", [])}
    raw = _load(dependencies_path)
    problems: list[str] = []

    edges: list[ElementEdgeSpec] = []
    for item in raw.get("element_edges", []):
        spec = ElementEdgeSpec(
            from_key=str(item["from"]),
            to_key=str(item["to"]),
            relation=str(item.get("relation", "connects_via")),
            redundancy_group=item.get("redundancy_group"),
            preferred=bool(item.get("preferred", False)),
        )
        for key in (spec.from_key, spec.to_key):
            if key not in names:
                problems.append(f"element key {key!r} is not in inventory_template.yaml")
        if spec.relation not in RELATIONS:
            problems.append(f"relation {spec.relation!r} must be one of {RELATIONS}")
        if spec.redundancy_group is not None and not SLUG.fullmatch(spec.redundancy_group):
            problems.append(f"redundancy group {spec.redundancy_group!r} must be a lowercase code")
        if spec.preferred and spec.redundancy_group is None:
            problems.append(f"{spec.from_key} -> {spec.to_key}: preferred needs a group")
        edges.append(spec)

    services: list[DeviceServiceSpec] = []
    for item in raw.get("device_services", []):
        spec = DeviceServiceSpec(device_type=str(item["device_type"]), service=str(item["service"]))
        if spec.device_type not in device_types:
            problems.append(f"device type {spec.device_type!r} is not in inventory_template.yaml")
        if spec.service not in catalogue.services:
            problems.append(f"service {spec.service!r} is not in the catalogue")
        services.append(spec)

    for spec in edges:
        for key in (spec.from_key, spec.to_key):
            if key in types and types[key] not in catalogue.types:
                problems.append(f"element type {types[key]!r} is not in the catalogue")
    if problems:
        raise DependencyTemplateError("; ".join(sorted(set(problems))))
    return DependencyTemplate(tuple(edges), tuple(services), names)
