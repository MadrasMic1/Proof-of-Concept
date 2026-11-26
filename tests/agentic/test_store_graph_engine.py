# tests/agentic/test_store_graph_engine.py
"""Phase 2: the generic graph engine on small hand-built graphs (no database)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.agentic.common.types import NodeKind, NodeStatus
from app.agentic.store_graph.engine import GroupState, analyse
from app.agentic.store_graph.model import GraphEdge, GraphError, GraphNode, StoreGraph

T0 = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)
WINDOW = timedelta(minutes=15)


def node(
    name: str,
    status: str | None = "online",
    *,
    minutes_ago: int = 60,
    kind: str = "device",
    criticality: str = "medium",
) -> GraphNode:
    node_id = name if kind == "service" else str(uuid.uuid5(uuid.NAMESPACE_URL, name))
    return GraphNode(
        key=name,
        kind=NodeKind(kind),
        id=node_id,
        name=name,
        type_code="x",
        label=name,
        status=NodeStatus(status) if status else None,
        status_since=T0 - timedelta(minutes=minutes_ago),
        criticality=criticality,
    )


def graph(nodes: list[GraphNode], edges: list[tuple]) -> StoreGraph:
    built = []
    for edge in edges:
        source, target, *rest = edge
        group = rest[0] if rest else None
        preferred = bool(rest[1]) if len(rest) > 1 else False
        built.append(GraphEdge(source, target, "connects_via", group, preferred))
    return StoreGraph({n.key: n for n in nodes}, built)


def run(g: StoreGraph, minimum: int = 3):
    return analyse(g, max_depth=6, common_cause_min=minimum, common_cause_window=WINDOW)


def roots(g: StoreGraph, minimum: int = 3) -> dict[str, tuple[str, tuple[str, ...]]]:
    return {r.key: (r.mode, r.impacted) for r in run(g, minimum).roots}


def test_single_device_down_is_its_own_root() -> None:
    g = graph([node("sw"), node("pos", "offline")], [("pos", "sw")])
    assert roots(g) == {"pos": ("down", ())}


def test_switch_down_folds_the_devices_behind_it() -> None:
    g = graph(
        [
            node("core"),
            node("sw", "offline"),
            node("ap", "offline"),
            node("pc", "offline"),
            node("gun", "offline"),
            node("pos"),
        ],
        [("sw", "core"), ("ap", "sw"), ("pc", "sw"), ("gun", "ap"), ("pos", "core")],
    )
    assert roots(g) == {"sw": ("down", ("ap", "gun", "pc"))}


def test_online_device_behind_a_failed_node_is_not_impacted() -> None:
    g = graph([node("ap", "offline"), node("gun")], [("gun", "ap")])
    assert roots(g) == {"ap": ("down", ())}


def test_degraded_dependency_does_not_explain_an_offline_device() -> None:
    g = graph([node("sw", "degraded"), node("pos", "offline")], [("pos", "sw")])
    assert roots(g) == {"sw": ("degraded", ()), "pos": ("down", ())}


def _wan(pri: str, bkp: str) -> StoreGraph:
    return graph(
        [
            node("gw"),
            node("pri", pri),
            node("bkp", bkp),
            node("pay", None, kind="service"),
            node("pos"),
        ],
        [
            ("gw", "pri", "wan", True),
            ("gw", "bkp", "wan"),
            ("pay", "pri", "wan", True),
            ("pay", "bkp", "wan"),
            ("pos", "pay"),
        ],
    )


def test_failover_preferred_link_down() -> None:
    analysis = run(_wan("offline", "online"))
    assert analysis.group_states[("gw", "wan")] == GroupState.FAILOVER
    assert analysis.effective["pay"] == NodeStatus.DEGRADED
    [root] = analysis.roots
    assert (root.key, root.mode) == ("pri", "failover")
    assert (root.impacted, root.remaining) == (("pay",), ("bkp",))


def test_backup_link_down_only_reduces_resilience() -> None:
    analysis = run(_wan("online", "offline"))
    assert analysis.group_states[("gw", "wan")] == GroupState.REDUCED
    assert analysis.effective["pay"] == NodeStatus.ONLINE
    assert [(r.key, r.mode, r.impacted) for r in analysis.roots] == [("bkp", "reduced", ())]


def test_both_links_down_merge_into_one_group_root() -> None:
    analysis = run(_wan("offline", "offline"))
    assert analysis.effective["pay"] == NodeStatus.OFFLINE
    [root] = analysis.roots
    assert (root.key, root.mode, root.group) == ("pri", "group_down", "wan")
    assert set(root.impacted) == {"bkp", "pay"}


def test_unknown_is_never_a_root_and_explains_nothing() -> None:
    g = graph([node("sw", "unknown"), node("pos", "offline")], [("pos", "sw")])
    analysis = run(g)
    assert [r.key for r in analysis.roots] == ["pos"]
    assert analysis.unknown == ["sw"]


def test_common_cause_suspect() -> None:
    nodes = [node("sw")] + [node(f"pos{i}", "offline", minutes_ago=30 + i) for i in range(3)]
    g = graph(nodes, [(f"pos{i}", "sw") for i in range(3)])
    [suspect] = run(g).suspects
    assert suspect.key == "sw" and suspect.failing == ("pos0", "pos1", "pos2")
    assert run(g, minimum=4).suspects == []


def test_common_cause_needs_the_window() -> None:
    nodes = [node("sw")] + [node(f"pos{i}", "offline", minutes_ago=10 + 20 * i) for i in range(3)]
    g = graph(nodes, [(f"pos{i}", "sw") for i in range(3)])
    assert run(g).suspects == []


def test_invalid_graphs_are_refused() -> None:
    with pytest.raises(GraphError, match="loop"):
        graph([node("a"), node("b")], [("a", "b"), ("b", "a")])
    with pytest.raises(GraphError, match="unknown node"):
        graph([node("a")], [("a", "missing")])
    with pytest.raises(GraphError, match="preferred"):
        graph([node("a"), node("b"), node("c")], [("a", "b", "g", True), ("a", "c", "g", True)])
