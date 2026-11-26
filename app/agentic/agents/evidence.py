# app/agentic/agents/evidence.py
"""Read this run's tool results: what the validators may accept and what code uses
to build the result bodies."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from app.agentic.agents.bridge import ToolCallRecord

NODE_KEYS = {"kind", "id", "name", "element_type", "label"}


def ok_data(records: Sequence[ToolCallRecord], *tools: str) -> list[dict[str, Any]]:
    return [
        r.envelope["data"] for r in records if r.tool in tools and r.ok and r.envelope.get("data")
    ]


def last_data(records: Sequence[ToolCallRecord], *tools: str) -> tuple[str, dict[str, Any]] | None:
    for record in reversed(records):
        if record.tool in tools and record.ok and record.envelope.get("data"):
            return record.tool, record.envelope["data"]
    return None


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def nodes_seen(records: Sequence[ToolCallRecord]) -> dict[str, dict[str, Any]]:
    """Every node reference (NodeRef shape) in this run's successful tool results."""
    found: dict[str, dict[str, Any]] = {}
    for record in records:
        if not record.ok:
            continue
        for item in _walk(record.envelope.get("data")):
            if isinstance(item, dict) and NODE_KEYS <= set(item):
                found.setdefault(str(item["id"]), {k: item[k] for k in NODE_KEYS})
    return found


def findings_seen(records: Sequence[ToolCallRecord]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for data in ok_data(records, "store_get_findings"):
        for finding in data.get("findings") or []:
            found.setdefault(finding["finding_id"], finding)
    return found


def incidents_seen(records: Sequence[ToolCallRecord]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for data in ok_data(records, "itsm_search_incidents", "store_get_node_status"):
        for item in [*(data.get("incidents") or []), *(data.get("open_incidents") or [])]:
            found.setdefault(item["number"], item)
    for data in ok_data(records, "itsm_get_incident"):
        found[data["number"]] = data
    return found


def flapping_seen(records: Sequence[ToolCallRecord]) -> bool:
    return any(d.get("flapping") for d in ok_data(records, "store_get_node_history"))
