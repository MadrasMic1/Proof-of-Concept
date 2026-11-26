# app/agentic/store_graph/engine.py
"""Where failures start and what they affect. Pure and generic: it never looks at
type codes, only at statuses and edges.

Rules:
- A node with its own status keeps it. A device that reports online is fine even
  if something it depends on is down (e.g. a handheld that roamed to another AP).
- A service (no status of its own) takes its status from its dependencies:
  a required dependency offline -> offline, degraded -> degraded; its group fully
  down -> offline, preferred member down (failover) -> degraded, another member down
  (reduced resilience) -> online.
- A failing node (offline or degraded) is EXPLAINED when a dependency it needs is
  offline (or, for a degraded node, degraded), or a group it needs is fully down.
  Unknown dependencies explain nothing: we do not guess.
- A failing node that is not explained is a ROOT CAUSE; every failing node and
  service it explains is folded under it. Unknown nodes are never root causes.
- When every member of a redundancy group is down, their root causes merge into
  one "group_down" root on the preferred member.
- Common-cause suspect: at least N roots that went offline within a window and
  need the same dependency that still reports online -> that dependency is suspect.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Literal

from app.agentic.common.types import NodeKind, NodeStatus
from app.agentic.store_graph.model import GraphEdge, StoreGraph

FAILING = frozenset({NodeStatus.OFFLINE, NodeStatus.DEGRADED})

RootMode = Literal["down", "degraded", "failover", "reduced", "group_down"]


class GroupState(StrEnum):
    OK = "ok"
    REDUCED = "reduced"  # a non-preferred member is down
    FAILOVER = "failover"  # the preferred member is down, another member is up
    DOWN = "down"  # every member is down


@dataclass(frozen=True)
class RootCause:
    key: str
    mode: RootMode
    impacted: tuple[str, ...]  # failing nodes and services it explains, by name
    group: str | None = None
    group_members: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()  # group members still online


@dataclass(frozen=True)
class Suspect:
    key: str
    failing: tuple[str, ...]


@dataclass(frozen=True)
class Analysis:
    effective: dict[str, NodeStatus]
    roots: list[RootCause]
    unknown: list[str]
    suspects: list[Suspect]
    group_states: dict[tuple[str, str], GroupState] = field(default_factory=dict)


def analyse(
    graph: StoreGraph,
    *,
    max_depth: int,
    common_cause_min: int,
    common_cause_window: timedelta,
) -> Analysis:
    return _Analyser(graph, max_depth).run(common_cause_min, common_cause_window)


class _Analyser:
    def __init__(self, graph: StoreGraph, max_depth: int) -> None:
        self.graph = graph
        self.max_depth = max_depth
        self._effective: dict[str, NodeStatus] = {}
        self._explain: dict[str, frozenset[str]] = {}
        self.group_states: dict[tuple[str, str], GroupState] = {}

    # --- statuses ------------------------------------------------------------------

    def effective(self, key: str, depth: int = 0) -> NodeStatus:
        if key in self._effective:
            return self._effective[key]
        node = self.graph.nodes[key]
        if node.status is not None:
            status = node.status
        elif depth >= self.max_depth:
            status = NodeStatus.UNKNOWN
        else:
            status = self._service_status(key, depth)
        self._effective[key] = status
        return status

    def _service_status(self, key: str, depth: int) -> NodeStatus:
        worst = NodeStatus.ONLINE
        for target in self.graph.required.get(key, []):
            status = self.effective(target, depth + 1)
            if status == NodeStatus.OFFLINE:
                return NodeStatus.OFFLINE
            if status == NodeStatus.DEGRADED:
                worst = NodeStatus.DEGRADED
        for group in self.graph.groups.get(key, {}):
            state = self.group_state(key, group, depth)
            if state == GroupState.DOWN:
                return NodeStatus.OFFLINE
            if state == GroupState.FAILOVER:
                worst = NodeStatus.DEGRADED
        return worst

    def group_state(self, source: str, group: str, depth: int = 0) -> GroupState:
        cache_key = (source, group)
        if cache_key in self.group_states:
            return self.group_states[cache_key]
        members = self.graph.groups[source][group]
        down = [m for m in members if self.effective(m.target, depth + 1) == NodeStatus.OFFLINE]
        if len(down) == len(members):
            state = GroupState.DOWN
        elif any(m.preferred for m in down):
            state = GroupState.FAILOVER
        elif down:
            state = GroupState.REDUCED
        else:
            state = GroupState.OK
        self.group_states[cache_key] = state
        return state

    # --- explanations ----------------------------------------------------------------

    def is_failing(self, key: str) -> bool:
        return self.effective(key) in FAILING

    def roots_of(self, key: str, depth: int = 0) -> frozenset[str]:
        """The root causes behind a failing node (itself when nothing explains it)."""
        explained = self.explain(key, depth)
        return explained or frozenset({key})

    def explain(self, key: str, depth: int = 0) -> frozenset[str]:
        if key in self._explain:
            return self._explain[key]
        result: set[str] = set()
        if depth < self.max_depth and self.is_failing(key):
            own = self.effective(key)
            for target in self.graph.required.get(key, []):
                status = self.effective(target)
                explains = status == NodeStatus.OFFLINE or (
                    own == NodeStatus.DEGRADED and status == NodeStatus.DEGRADED
                )
                if explains:
                    result |= self.roots_of(target, depth + 1)
            for group, members in self.graph.groups.get(key, {}).items():
                if self.group_state(key, group) == GroupState.DOWN:
                    for member in members:
                        result |= self.roots_of(member.target, depth + 1)
        self._explain[key] = frozenset(result)
        return self._explain[key]

    def _service_causes(self, key: str) -> set[str]:
        causes: set[str] = set()
        for target in self.graph.required.get(key, []):
            if self.is_failing(target):
                causes |= self.roots_of(target)
        for group, members in self.graph.groups.get(key, {}).items():
            if self.group_state(key, group) in (GroupState.FAILOVER, GroupState.DOWN):
                for member in members:
                    if self.effective(member.target) == NodeStatus.OFFLINE:
                        causes |= self.roots_of(member.target)
        return causes

    # --- the run ---------------------------------------------------------------------

    def run(self, common_cause_min: int, window: timedelta) -> Analysis:
        nodes = self.graph.nodes
        for key in nodes:
            self.effective(key)
        for source, groups in self.graph.groups.items():
            for group in groups:
                self.group_state(source, group)
        root_keys: set[str] = set()
        impacted: dict[str, set[str]] = defaultdict(set)
        for key, node in nodes.items():
            if node.kind == NodeKind.SERVICE or not self.is_failing(key):
                continue
            causes = self.explain(key)
            if not causes:
                root_keys.add(key)
            for cause in causes:
                impacted[cause].add(key)
        for key, node in nodes.items():
            if node.kind == NodeKind.SERVICE and self.is_failing(key):
                for cause in self._service_causes(key):
                    impacted[cause].add(key)
        roots = self._build_roots(root_keys, impacted)
        unknown = sorted(
            (k for k, n in nodes.items() if n.status == NodeStatus.UNKNOWN),
            key=lambda k: nodes[k].name,
        )
        suspects = self._suspects(root_keys, common_cause_min, window)
        return Analysis(
            effective=dict(self._effective),
            roots=roots,
            unknown=unknown,
            suspects=suspects,
            group_states=dict(self.group_states),
        )

    def _by_name(self, keys: set[str] | frozenset[str]) -> tuple[str, ...]:
        return tuple(sorted(keys, key=lambda k: (self.graph.nodes[k].name, k)))

    def _membership(self, key: str) -> tuple[str, str, list[GraphEdge]] | None:
        """(source, group, members) of the first group this node is a member of."""
        for source, groups in self.graph.groups.items():
            for group, members in groups.items():
                if any(m.target == key for m in members):
                    return source, group, members
        return None

    def _build_roots(self, root_keys: set[str], impacted: dict[str, set[str]]) -> list[RootCause]:
        merged: set[str] = set()
        roots: list[RootCause] = []
        for (source, group), state in sorted(self.group_states.items()):
            if state != GroupState.DOWN:
                continue
            members = self.graph.groups[source][group]
            member_keys = [m.target for m in members]
            if not all(k in root_keys for k in member_keys) or merged & set(member_keys):
                continue
            preferred = next((m.target for m in members if m.preferred), None)
            head = preferred or self._by_name(set(member_keys))[0]
            folded: set[str] = set()
            for k in member_keys:
                folded |= impacted.get(k, set())
            folded |= set(member_keys) - {head}
            merged |= set(member_keys)
            roots.append(
                RootCause(
                    key=head,
                    mode="group_down",
                    impacted=self._by_name(folded - {head}),
                    group=group,
                    group_members=self._by_name(set(member_keys)),
                )
            )
        for key in self._by_name(root_keys - merged):
            own = self.effective(key)
            mode: RootMode = "degraded" if own == NodeStatus.DEGRADED else "down"
            group = None
            remaining: tuple[str, ...] = ()
            membership = self._membership(key)
            if membership is not None and own == NodeStatus.OFFLINE:
                source, group, members = membership
                state = self.group_states.get((source, group), GroupState.OK)
                if state in (GroupState.FAILOVER, GroupState.REDUCED):
                    preferred = any(m.preferred and m.target == key for m in members)
                    mode = "failover" if preferred else "reduced"
                    remaining = self._by_name(
                        {
                            m.target
                            for m in members
                            if self.effective(m.target) != NodeStatus.OFFLINE
                        }
                    )
                else:
                    group = None
            roots.append(
                RootCause(
                    key=key,
                    mode=mode,
                    impacted=self._by_name(impacted.get(key, set())),
                    group=group,
                    remaining=remaining,
                )
            )
        return roots

    def _suspects(self, root_keys: set[str], minimum: int, window: timedelta) -> list[Suspect]:
        nodes = self.graph.nodes
        by_dependency: dict[str, list[str]] = defaultdict(list)
        for key in root_keys:
            if self.effective(key) != NodeStatus.OFFLINE or nodes[key].status_since is None:
                continue
            for target in self.graph.required.get(key, []):
                if self.effective(target) == NodeStatus.ONLINE:
                    by_dependency[target].append(key)
        suspects: list[Suspect] = []
        for target, keys in sorted(by_dependency.items()):
            keys.sort(key=lambda k: nodes[k].status_since)
            best: list[str] = []
            start = 0
            for end in range(len(keys)):
                while nodes[keys[end]].status_since - nodes[keys[start]].status_since > window:
                    start += 1
                if end - start + 1 > len(best):
                    best = keys[start : end + 1]
            if len(best) >= minimum:
                suspects.append(Suspect(key=target, failing=self._by_name(set(best))))
        return suspects
