# tests/agentic/test_store_scenarios.py
"""Phase 2: every seeded scenario, built from the seed files (no database).

The same expectations are checked against the seeded test database in
test_store_snapshot_db.py, so the graph, the seed and the app screens agree.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agentic.config import AgenticSettings
from app.agentic.store_context.assemble import assemble_snapshot
from app.agentic.store_graph.catalogue import load_catalogue
from tests.agentic.seed_scenario import ROOT, all_sites, seed_state

ANCHOR = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)  # conftest.ITSM_ANCHOR
CATALOGUE = load_catalogue(ROOT / "config" / "element_catalogue.yaml")

# site -> {root name: (severity, status, fault_code, staff_can_fix, incident, impacted)}
EXPECTED: dict[str, dict[str, tuple]] = {
    "01234": {
        "WAN01234-PRI": ("major", "offline", None, False, "INC00001234", ["Card payments"]),
        "POS Terminal 03": ("major", "offline", "HW_FAULT", False, "INC00001235", []),
        "Register 04": ("major", "offline", "NOT_REACHABLE", True, "INC00001236", []),
        "AP01234N004": ("minor", "offline", None, False, "INC00001237", []),
    },
    "01456": {
        "WAN01456-BKP": ("minor", "offline", None, False, "INC00001301", []),
        "Printer 02": ("minor", "degraded", "PAPER_JAM", True, "INC00001302", []),
    },
    "02556": {
        "SW02556N303": (
            "critical",
            "offline",
            None,
            False,
            "INC00001303",
            ["AP02556N003", "AP02556N005", "RF Gun 04", "Workstation 01", "Workstation 02"],
        ),
    },
    "03340": {
        "Printer 01": ("minor", "offline", "POWER_OFF", True, "INC00001304", []),
        "RF Gun 02": ("minor", "degraded", "BATTERY_LOW", True, None, []),
    },
}


def snapshot(site: str):
    settings = AgenticSettings(_env_file=None, environment="test")
    state = seed_state(site, ANCHOR, CATALOGUE)
    return assemble_snapshot(state, CATALOGUE, now=ANCHOR, settings=settings)


def actual(site: str) -> dict[str, tuple]:
    return {
        f.root_node.name: (
            f.severity,
            f.status.value,
            f.fault_code,
            f.staff_can_fix,
            f.covered_by_incident,
            [n.name for n in f.impacted_nodes],
        )
        for f in snapshot(site).findings
        if f.kind == "root_cause"
    }


@pytest.mark.parametrize("site", all_sites())
def test_seeded_store(site: str) -> None:
    assert actual(site) == EXPECTED.get(site, {})


def test_failover_text_and_resolved_incident() -> None:
    snap = snapshot("01234")
    wan = next(f for f in snap.findings if f.root_node.name == "WAN01234-PRI")
    assert "running on Backup ISP link WAN01234-BKP" in wan.impact_text
    assert [i.number for i in snap.recently_resolved] == ["INC00001238"]
    assert {i.number for i in snap.incidents} == {
        "INC00001234",
        "INC00001235",
        "INC00001236",
        "INC00001237",
    }


def test_flapping_ap_is_healthy_now() -> None:
    snap = snapshot("03118")
    assert snap.findings == []
    ap = next(v for v in snap.nodes if v.node.name == "AP03118N002")
    assert ap.reported == "online"


def test_finding_ids_are_stable() -> None:
    first = [f.finding_id for f in snapshot("01234").findings]
    assert first == [f.finding_id for f in snapshot("01234").findings]
    assert len(set(first)) == len(first)


def test_new_element_type_through_configuration_only(tmp_path: Path) -> None:
    """A kitchen display (new type) behind a new controller: catalogue + edges, no code."""
    import dataclasses
    import uuid

    from app.agentic.store_context.rows import DeviceRow, ElementRow
    from app.agentic.store_graph.catalogue import Catalogue

    raw = CATALOGUE.model_dump()
    raw["types"]["kds_controller"] = {
        **raw["types"]["switch"],
        "label": "Kitchen controller",
        "synonyms": ["kitchen controller"],
    }
    raw["types"]["kitchen_display"] = {
        **raw["types"]["workstation"],
        "label": "Kitchen display",
        "synonyms": ["kitchen screen", "kds"],
        "impact_when_down": "{label} is blank: orders do not reach the kitchen.",
        "faults": [],
    }
    catalogue = Catalogue.model_validate(raw)
    state = seed_state("01678", ANCHOR, catalogue)
    core = next(e for e in state.elements if e.name == "SW01678N301")
    controller = ElementRow(
        id=uuid.uuid4(),
        name="KDS01678N001",
        type_code="kds_controller",
        role=None,
        location="Kitchen",
        uplink_element_id=core.id,
        criticality="high",
        status="offline",
        status_since=ANCHOR,
        last_event_at=ANCHOR,
    )
    display = DeviceRow(
        id=uuid.uuid4(),
        name="Kitchen Display 01",
        type_code="kitchen_display",
        location="Kitchen",
        connection_type="wired",
        connected_element_id=controller.id,
        criticality="medium",
        status="offline",
        status_since=ANCHOR,
        status_reason=None,
        last_heartbeat_at=None,
    )
    state = dataclasses.replace(
        state, elements=[*state.elements, controller], devices=[*state.devices, display]
    )
    settings = AgenticSettings(_env_file=None, environment="test")
    snap = assemble_snapshot(state, catalogue, now=ANCHOR, settings=settings)
    [finding] = snap.findings
    assert finding.root_node.name == "KDS01678N001"
    assert [n.name for n in finding.impacted_nodes] == ["Kitchen Display 01"]
    assert finding.severity == "major" and not finding.staff_can_fix
