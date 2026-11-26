# app/agentic/tools/store.py
"""Store and graph tools (Store Insight): findings, device resolution, node status and
history, and three graph walks. Read-only; everything comes from the live snapshot
and graph of the token's store (Agentic Phase 2)."""

from __future__ import annotations

from collections import deque
from datetime import timedelta
from typing import Any, Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.common.types import NodeKind
from app.agentic.store_context.resolve import resolve_device
from app.agentic.store_graph.findings import SEVERITY_RANK
from app.agentic.store_graph.model import GraphEdge, StoreGraph
from app.agentic.tools.base import (
    Args,
    LoadedStore,
    ToolContext,
    ToolFailure,
    ToolOutput,
    load_store,
)
from app.agentic.values import ErrorCode
from app.models import NetworkEvent

MAX_GRAPH_NODES = 30


class FindingsArgs(Args):
    severity_min: Literal["critical", "major", "minor", "info"] | None = None
    include_suspects: bool = True


class ResolveArgs(Args):
    text: str = Field(min_length=1, max_length=100)


class NodeArgs(Args):
    node_id: str = Field(min_length=1, max_length=64)


class HistoryArgs(Args):
    node_id: str = Field(min_length=1, max_length=64)
    hours: int | None = Field(default=None, ge=1, le=24)


class NeighbourhoodArgs(Args):
    node_id: str = Field(min_length=1, max_length=64)
    depth: int = Field(default=1, ge=1, le=2)


class PathArgs(Args):
    from_node_id: str = Field(min_length=1, max_length=64)
    to_node_id: str = Field(min_length=1, max_length=64)


def _store_info(loaded: LoadedStore) -> dict[str, Any]:
    store = loaded.snapshot.store
    return {
        "site_code": store.site_code,
        "name": store.name,
        "local_time": store.local_time.isoformat(),
        "open_now": store.open_now,
    }


def _graph(loaded: LoadedStore) -> StoreGraph:
    if loaded.graph is None:
        raise ToolFailure(ErrorCode.UNAVAILABLE, "the store graph is not usable right now")
    return loaded.graph


def _key(graph: StoreGraph, node_id: str) -> str:
    for key, node in graph.nodes.items():
        if node.id == node_id:
            return key
    raise ToolFailure(ErrorCode.NOT_FOUND, f"node {node_id} is not in this store")


def _node_info(loaded: LoadedStore, graph: StoreGraph, key: str) -> dict[str, Any]:
    node = graph.nodes[key]
    view = loaded.snapshot.node(node.id)
    return {
        "node": node.ref().model_dump(mode="json"),
        "status": str(view.effective) if view else "unknown",
        "since": view.since.isoformat() if view and view.since else None,
    }


def _edge_map(graph: StoreGraph) -> dict[tuple[str, str], GraphEdge]:
    return {(e.source, e.target): e for e in graph.edges}


async def store_get_findings(
    session: AsyncSession, ctx: ToolContext, args: FindingsArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    limit = SEVERITY_RANK[args.severity_min] if args.severity_min else len(SEVERITY_RANK)
    findings = [
        f
        for f in loaded.snapshot.findings
        if SEVERITY_RANK[f.severity] <= limit and (args.include_suspects or f.kind != "suspect")
    ]
    shown = findings[: ctx.settings.mcp_max_findings]
    return ToolOutput(
        {
            "store": _store_info(loaded),
            "findings": [f.model_dump(mode="json") for f in shown],
            "more": len(findings) - len(shown),
            "warnings": list(loaded.snapshot.warnings),
        },
        loaded.snapshot.as_of,
    )


async def store_resolve_device(
    session: AsyncSession, ctx: ToolContext, args: ResolveArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    candidates = resolve_device(
        args.text,
        loaded.snapshot.nodes,
        ctx.catalogue,
        limit=ctx.settings.store_insight_max_device_options,
    )
    return ToolOutput(
        {
            "candidates": [
                {
                    "node": c.node.model_dump(mode="json"),
                    "confidence": c.confidence,
                    "matched_on": c.matched_on,
                }
                for c in candidates
            ]
        },
        loaded.snapshot.as_of,
    )


async def store_get_node_status(
    session: AsyncSession, ctx: ToolContext, args: NodeArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    view = loaded.snapshot.node(args.node_id)
    if view is None:
        raise ToolFailure(ErrorCode.NOT_FOUND, f"node {args.node_id} is not in this store")
    incidents = [
        i.model_dump(mode="json")
        for i in loaded.snapshot.incidents
        if i.affected is not None and i.affected.id == args.node_id
    ]
    return ToolOutput(
        {
            "node": view.node.model_dump(mode="json"),
            "reported": str(view.reported),
            "effective": str(view.effective),
            "since": view.since.isoformat() if view.since else None,
            "reason": view.reason,
            "location": view.location,
            "role": view.role,
            "stale": view.stale,
            "open_incidents": incidents,
        },
        loaded.snapshot.as_of,
        ["open_incidents[].short_description", "open_incidents[].eta_note"],
    )


async def store_get_node_history(
    session: AsyncSession, ctx: ToolContext, args: HistoryArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    view = loaded.snapshot.node(args.node_id)
    if view is None:
        raise ToolFailure(ErrorCode.NOT_FOUND, f"node {args.node_id} is not in this store")
    hours = args.hours or ctx.settings.store_insight_history_lookback_hours
    base = {"node": view.node.model_dump(mode="json"), "hours": hours}
    if view.node.kind == NodeKind.SERVICE:
        raise ToolFailure(ErrorCode.INVALID_INPUT, "services have no history; ask about a device")
    if view.node.kind == NodeKind.DEVICE:
        since = view.since.isoformat() if view.since else "unknown"
        return ToolOutput(
            {
                **base,
                "events": [],
                "flap_count": 0,
                "flapping": False,
                "note": f"devices keep only their current status: {view.reported} since {since}",
            },
            loaded.snapshot.as_of,
        )
    rows = (
        await session.execute(
            select(
                NetworkEvent.occurred_at,
                NetworkEvent.reported_status,
                NetworkEvent.vendor_event_type,
            )
            .where(
                NetworkEvent.store_id == ctx.store_id,
                NetworkEvent.network_element_id == view.node.id,
                NetworkEvent.status_applied,
                NetworkEvent.occurred_at >= ctx.now - timedelta(hours=hours),
                NetworkEvent.occurred_at <= ctx.now,
            )
            .order_by(NetworkEvent.occurred_at)
        )
    ).all()
    flaps = sum(
        1
        for before, after in zip(rows, rows[1:], strict=False)
        if before.reported_status != after.reported_status
    )
    shown = rows[-ctx.settings.mcp_max_history_events :]
    return ToolOutput(
        {
            **base,
            "events": [
                {
                    "at": r.occurred_at.isoformat(),
                    "status": r.reported_status,
                    "event_type": r.vendor_event_type,
                }
                for r in shown
            ],
            "flap_count": flaps,
            "flapping": flaps >= ctx.settings.store_insight_flap_threshold,
        },
        loaded.snapshot.as_of,
    )


async def graph_neighbourhood(
    session: AsyncSession, ctx: ToolContext, args: NeighbourhoodArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    graph = _graph(loaded)
    start = _key(graph, args.node_id)
    edges = _edge_map(graph)

    def walk(next_of: Any, edge_of: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen = {start}
        frontier = [(start, 0)]
        while frontier:
            key, distance = frontier.pop(0)
            if distance >= args.depth:
                continue
            for other in next_of(key):
                if other in seen or len(out) >= MAX_GRAPH_NODES:
                    continue
                seen.add(other)
                edge = edge_of(key, other)
                out.append(
                    {
                        **_node_info(loaded, graph, other),
                        "distance": distance + 1,
                        "relation": edge.relation if edge else None,
                        "redundancy_group": edge.group if edge else None,
                    }
                )
                frontier.append((other, distance + 1))
        return out

    depends_on = walk(graph.depends_on, lambda a, b: edges.get((a, b)))
    dependants = walk(lambda k: graph.dependants.get(k, []), lambda a, b: edges.get((b, a)))
    return ToolOutput(
        {
            "node": _node_info(loaded, graph, start),
            "depends_on": depends_on,
            "dependants": dependants,
        },
        loaded.snapshot.as_of,
    )


async def graph_impact_of(session: AsyncSession, ctx: ToolContext, args: NodeArgs) -> ToolOutput:
    """Everything that stops working if this node is down (required edges), and what
    only loses its backup (the node is one member of a redundancy group)."""
    loaded = await load_store(session, ctx)
    graph = _graph(loaded)
    start = _key(graph, args.node_id)
    edges = _edge_map(graph)
    impacted: list[str] = []
    at_risk: list[str] = []
    queue = deque([start])
    seen = {start}
    while queue:
        key = queue.popleft()
        for dependant in graph.dependants.get(key, []):
            if dependant in seen:
                continue
            seen.add(dependant)
            edge = edges.get((dependant, key))
            if edge is not None and edge.group is not None:
                at_risk.append(dependant)
                continue
            impacted.append(dependant)
            queue.append(dependant)
    return ToolOutput(
        {
            "node": _node_info(loaded, graph, start),
            "impacted": [_node_info(loaded, graph, k) for k in impacted[:MAX_GRAPH_NODES]],
            "impacted_count": len(impacted),
            "loses_backup": [_node_info(loaded, graph, k) for k in at_risk[:MAX_GRAPH_NODES]],
        },
        loaded.snapshot.as_of,
    )


def _path(graph: StoreGraph, start: str, goal: str) -> list[str] | None:
    previous: dict[str, str | None] = {start: None}
    queue = deque([start])
    while queue:
        key = queue.popleft()
        if key == goal:
            path = [key]
            parent = previous[key]
            while parent is not None:
                path.append(parent)
                parent = previous[parent]
            return list(reversed(path))
        for target in graph.depends_on(key):
            if target not in previous:
                previous[target] = key
                queue.append(target)
    return None


async def graph_explain_path(session: AsyncSession, ctx: ToolContext, args: PathArgs) -> ToolOutput:
    loaded = await load_store(session, ctx)
    graph = _graph(loaded)
    start, goal = _key(graph, args.from_node_id), _key(graph, args.to_node_id)
    direction = "depends_on"
    path = _path(graph, start, goal)
    if path is None:
        path = _path(graph, goal, start)
        direction = "needed_by"
    if path is None:
        return ToolOutput({"connected": False, "path": []}, loaded.snapshot.as_of)
    edges = _edge_map(graph)
    steps = []
    for index, key in enumerate(path):
        nxt = path[index + 1] if index + 1 < len(path) else None
        edge = edges.get((key, nxt)) if nxt else None
        steps.append(
            {
                **_node_info(loaded, graph, key),
                "relation": edge.relation if edge else None,
                "redundancy_group": edge.group if edge else None,
            }
        )
    return ToolOutput(
        {"connected": True, "direction": direction, "path": steps}, loaded.snapshot.as_of
    )
