# app/agentic/drafts/triage.py
"""Pure rules for drafts: triage baseline, clamping, text overlap, mentions, masking.

Impact and urgency use the incidents table scale: 1 = high, 2 = medium, 3 = low;
priority = impact + urgency - 1 (computed by the database).

Baseline impact
    1  the finding is critical (both members of a redundancy group down, or it
       explains at least INCIDENT_IMPACT_HIGH_MIN_NODES nodes: Phase 2 severity)
    2  the finding affects other nodes (incl. failover: card payments degraded)
    3  a single device, a reduced-resilience finding, or no finding
Baseline urgency
    1  the affected type is revenue_critical and the store is open now
    2  revenue_critical but the store is closed, or a guide ended needs_technician
    3  anything else
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.agentic.common.types import Finding

HIGH, MEDIUM, LOW = 1, 2, 3
STOPWORDS = frozenset(
    "the and for with from that this not but are was were has have had its our "
    "store till please again still any all can cannot can't does doesnt dont don't "
    "down issue problem working work".split()
)
_INCIDENT = re.compile(r"\bINC\d{8}\b", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"(?<![A-Za-z0-9])\+?\d[\d\s-]{6,}\d(?![A-Za-z0-9])")


@dataclass(frozen=True)
class Baseline:
    impact: int
    urgency: int
    impact_reason: str
    urgency_reason: str


@dataclass(frozen=True)
class Clamped:
    value: int
    adjusted: bool


def baseline(
    finding: Finding | None,
    *,
    revenue_critical: bool,
    store_open: bool,
    guide_needs_technician: bool,
) -> Baseline:
    if finding is None:
        impact, impact_reason = LOW, "no store-wide finding: a single report"
    elif finding.severity == "critical":
        impact, impact_reason = HIGH, "store-wide: critical finding"
    elif finding.impacted_nodes:
        names = ", ".join(n.name for n in finding.impacted_nodes[:4])
        impact, impact_reason = MEDIUM, f"also affects {names}"
    else:
        impact, impact_reason = LOW, f"single item: {finding.root_node.name}"
    if revenue_critical and store_open:
        urgency, urgency_reason = HIGH, "revenue-critical and the store is open"
    elif revenue_critical:
        urgency, urgency_reason = MEDIUM, "revenue-critical, store closed now"
    elif guide_needs_technician:
        urgency, urgency_reason = MEDIUM, "self-fix guide ended: needs a technician"
    else:
        urgency, urgency_reason = LOW, "not revenue-critical"
    return Baseline(impact, urgency, impact_reason, urgency_reason)


def clamp(proposed: int, base: int, max_deviation: int) -> Clamped:
    low, high = max(HIGH, base - max_deviation), min(LOW, base + max_deviation)
    value = min(max(proposed, low), high)
    return Clamped(value, value != proposed)


def words(text: str | None) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) >= 3 and w not in STOPWORDS
    }


def overlap(a: str | None, b: str | None) -> float:
    """Overlap coefficient of the content words: |A & B| / min(|A|, |B|)."""
    left, right = words(a), words(b)
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def incident_numbers(text: str) -> set[str]:
    return {m.upper() for m in _INCIDENT.findall(text)}


def mentioned_names(text: str, names: Iterable[str]) -> set[str]:
    """Which of names appear in text as whole phrases (case-insensitive)."""
    lowered = text.lower()
    found = set()
    for name in names:
        pattern = rf"(?<![a-z0-9]){re.escape(name.lower())}(?![a-z0-9])"
        if re.search(pattern, lowered):
            found.add(name)
    return found


def mask_text(value: str | None) -> str | None:
    """Mask e-mail addresses and long numbers (not incident numbers like INC00001234)."""
    if value is None:
        return None
    return _LONG_NUMBER.sub("[number]", _EMAIL.sub("[email]", value))
