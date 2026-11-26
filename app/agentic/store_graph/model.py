# app/agentic/store_graph/model.py
"""The store graph in memory: nodes (devices, network elements, services) and
"depends on" edges. Pure: no database, no clock.

An edge source -> target means "source needs target". Edges without a redundancy
group are required; edges sharing a group are alternatives (the source works while
any member works). Built fresh for every snapshot, so it is never out of date.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from app.agentic.common.types import NodeKind, NodeRef, NodeStatus


class GraphError(ValueError):
    """The edges do not form a valid graph (unknown node, loop, bad group)."""


def node_key(kind: NodeKind | str, node_id: str) -> str:
    return f"{NodeKind(kind).value}:{node_id}"


@dataclass(frozen=True)
class GraphNode:
    key: str
    kind: NodeKind
    id: str
    name: str
    type_code: str
    label: str
    status: NodeStatus | None  # own reported status (stale rule applied); None for services
    status_since: datetime | None = None
    reason: str | None = None
    role: str | None = None
    location: str | None = None
    criticality: str = "medium"  # low | medium | high (type business_criticality)
    stale: bool = False

    def ref(self) -> NodeRef:
        return NodeRef(
            kind=self.kind,
            id=self.id,
            name=self.name,
            element_type=self.type_code,
            label=self.label,
        )


@dataclass(frozen=True)
class GraphEdge:
    source: str
    target: str
    relation: str
    group: str | None = None
    preferred: bool = False
    derived: bool = False


class StoreGraph:
    def __init__(self, nodes: Mapping[str, GraphNode], edges: Sequence[GraphEdge]) -> None:
        self.nodes: dict[str, GraphNode] = dict(nodes)
        self.edges: list[GraphEdge] = []
        self.required: dict[str, list[str]] = defaultdict(list)
        self.groups: dict[str, dict[str, list[GraphEdge]]] = defaultdict(dict)
        self.dependants: dict[str, list[str]] = defaultdict(list)
        seen: set[tuple[str, str]] = set()
        for edge in edges:
            for end in (edge.source, edge.target):
                if end not in self.nodes:
                    raise GraphError(f"edge {edge.source} -> {edge.target}: unknown node {end}")
            if edge.source == edge.target:
                raise GraphError(f"edge {edge.source} depends on itself")
            if (edge.source, edge.target) in seen:
                continue
            seen.add((edge.source, edge.target))
            self.edges.append(edge)
            self.dependants[edge.target].append(edge.source)
            if edge.group is None:
                self.required[edge.source].append(edge.target)
            else:
                self.groups[edge.source].setdefault(edge.group, []).append(edge)
        for source, groups in self.groups.items():
            for group, members in groups.items():
                if sum(member.preferred for member in members) > 1:
                    raise GraphError(f"{source} group {group!r} has more than one preferred member")
        self._check_no_loops()

    def depends_on(self, key: str) -> list[str]:
        """Every direct dependency (required and group members)."""
        members = [edge.target for group in self.groups.get(key, {}).values() for edge in group]
        return [*self.required.get(key, []), *members]

    def group_members(self, group: str) -> list[GraphEdge]:
        """The member edges of every group with this name (any source)."""
        return [
            edge
            for groups in self.groups.values()
            for name, members in groups.items()
            if name == group
            for edge in members
        ]

    def _check_no_loops(self) -> None:
        state: dict[str, int] = {}  # 1 visiting, 2 done

        def visit(key: str, path: list[str]) -> None:
            state[key] = 1
            for target in self.depends_on(key):
                if state.get(target) == 1:
                    loop = " -> ".join([*path, key, target])
                    raise GraphError(f"dependency loop: {loop}")
                if target not in state:
                    visit(target, [*path, key])
            state[key] = 2

        for key in self.nodes:
            if key not in state:
                visit(key, [])
