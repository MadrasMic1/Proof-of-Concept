# tests/agentic/test_draft_triage.py
"""Agentic Phase 4: the pure draft rules (no database)."""

from __future__ import annotations

import uuid

import pytest

from app.agentic.common.types import Finding, NodeKind, NodeRef, NodeStatus
from app.agentic.drafts.triage import (
    baseline,
    clamp,
    incident_numbers,
    mask_text,
    mentioned_names,
    overlap,
)


def _ref(name: str) -> NodeRef:
    return NodeRef(
        kind=NodeKind.DEVICE,
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, name)),
        name=name,
        element_type="printer",
        label=name,
    )


def _finding(severity: str, impacted: int = 0) -> Finding:
    return Finding(
        finding_id="F-1",
        kind="root_cause",
        severity=severity,
        root_node=_ref("Printer 01"),
        status=NodeStatus.OFFLINE,
        impacted_nodes=[_ref(f"Item {i}") for i in range(impacted)],
        impact_text="x",
        staff_can_fix=False,
    )


@pytest.mark.parametrize(
    ("finding", "critical", "open_now", "guide", "expected"),
    [
        (_finding("critical", 5), True, True, False, (1, 1)),
        (_finding("major", 1), True, False, False, (2, 2)),
        (_finding("minor"), False, True, False, (3, 3)),
        (_finding("minor"), False, True, True, (3, 2)),
        (None, False, True, False, (3, 3)),
    ],
)
def test_baseline(
    finding: Finding | None, critical: bool, open_now: bool, guide: bool, expected: tuple
) -> None:
    base = baseline(
        finding, revenue_critical=critical, store_open=open_now, guide_needs_technician=guide
    )
    assert (base.impact, base.urgency) == expected
    assert base.impact_reason and base.urgency_reason


@pytest.mark.parametrize(
    ("proposed", "base", "deviation", "expected"),
    [
        (1, 3, 1, (2, True)),
        (3, 1, 1, (2, True)),
        (2, 2, 1, (2, False)),
        (1, 3, 2, (1, False)),
        (3, 3, 0, (3, False)),
        (1, 2, 0, (2, True)),
    ],
)
def test_clamp(proposed: int, base: int, deviation: int, expected: tuple) -> None:
    result = clamp(proposed, base, deviation)
    assert (result.value, result.adjusted) == expected


def test_overlap() -> None:
    assert overlap("Printer 02 - Paper Jam", "Printer 02 paper jammed again") == pytest.approx(
        2 / 3
    )
    assert overlap("Printer 02 - Paper Jam", "Paper jam on printer") == 1.0
    assert overlap("Wi-Fi slow", "Printer jam") == 0.0
    assert overlap("", "Printer") == 0.0


def test_mentions_and_numbers() -> None:
    text = "POS Terminal 03 fails, see inc00001235; Printer 0 is fine. POS Terminal 3?"
    names = {"POS Terminal 03", "POS Terminal 3", "Printer 01"}
    assert mentioned_names(text, names) == {"POS Terminal 03", "POS Terminal 3"}
    assert incident_numbers(text) == {"INC00001235"}


def test_mask_text_keeps_incident_numbers() -> None:
    masked = mask_text("Call +91 98765 43210 or a.b@tcs.com about INC00001234")
    assert masked == "Call [number] or [email] about INC00001234"
    assert mask_text(None) is None
