# app/agentic/store_context/resolve.py
"""Map a user's words ("till 2 printer", "pos 3", "the wifi") to store nodes.

Scores (highest per node wins):
    1.00 name     the exact device / element name ("POS Terminal 03")
    0.95 name     the name appears in the text
    0.85 number   type synonym + the device number ("printer 2" -> Printer 02)
    0.80 location type synonym + a location number ("checkout 3 pos", "till 3 register")
    0.70 number   type + a location number, when no device of the type has that
                  location (printers sit in "Checkout area": "till 2 printer" -> Printer 02)
    0.60 type     type only, and the store has one node of that type
    0.50 location location number only ("till 3": everything at Checkout 3)
    0.30 type     type only, several nodes of that type
Location words: till, checkout, lane, counter (they match "Checkout N").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.agentic.common.types import NodeKind, NodeRef
from app.agentic.store_context.assemble import NodeView
from app.agentic.store_graph.catalogue import Catalogue

LOCATION_WORDS = ("till", "checkout", "lane", "counter")
MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class Candidate:
    node: NodeRef
    confidence: float
    matched_on: str


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9\- ]+", " ", text.lower()).split())


def _contains(text: str, phrase: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", text) is not None


def _trailing_number(value: str) -> int | None:
    match = re.search(r"(\d+)\s*$", value)
    return int(match.group(1)) if match else None


def _location_number(location: str | None) -> int | None:
    if not location:
        return None
    match = re.search(r"checkout\s*(\d+)", location.lower())
    return int(match.group(1)) if match else None


def resolve_device(
    text: str,
    nodes: Sequence[NodeView],
    catalogue: Catalogue,
    *,
    limit: int,
) -> list[Candidate]:
    query = _norm(text)
    if not query:
        return []
    views = [v for v in nodes if v.node.kind in (NodeKind.DEVICE, NodeKind.NETWORK_ELEMENT)]
    scores: dict[str, tuple[float, str, NodeRef]] = {}

    def score(view: NodeView, value: float, how: str) -> None:
        best = scores.get(view.node.id)
        if best is None or value > best[0]:
            scores[view.node.id] = (value, how, view.node)

    for view in views:
        name = _norm(view.node.name)
        if query == name:
            score(view, 1.0, "name")
        elif _contains(query, name):
            score(view, 0.95, "name")

    location_numbers: set[int] = set()
    for word in LOCATION_WORDS:
        location_numbers |= {int(n) for n in re.findall(rf"\b{word}\s*(\d+)", query)}
    other_numbers = {int(n) for n in re.findall(r"\d+", query)} - location_numbers

    types: list[str] = []
    for code, entry in catalogue.types.items():
        if any(_contains(query, synonym) for synonym in entry.synonyms):
            types.append(code)

    for code in types:
        of_type = [v for v in views if v.node.element_type == code]
        located = [v for v in of_type if _location_number(v.location) in location_numbers]
        numbered = [v for v in of_type if _trailing_number(v.node.name) in other_numbers]
        for view in numbered:
            score(view, 0.85, "number")
        for view in located:
            score(view, 0.8, "location")
        by_location_number = []
        if location_numbers and not located and not numbered:
            by_location_number = [
                v for v in of_type if _trailing_number(v.node.name) in location_numbers
            ]
            for view in by_location_number:
                score(view, 0.7, "number")
        if not numbered and not located and not by_location_number:
            for view in of_type:
                score(view, 0.6 if len(of_type) == 1 else 0.3, "type")

    if location_numbers and not types:
        for view in views:
            if _location_number(view.location) in location_numbers:
                score(view, 0.5, "location")

    ranked = sorted(scores.values(), key=lambda item: (-item[0], item[2].name))
    return [
        Candidate(node=node, confidence=value, matched_on=how)
        for value, how, node in ranked
        if value >= MIN_CONFIDENCE
    ][:limit]
