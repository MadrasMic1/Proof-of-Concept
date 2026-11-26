# tests/agentic/test_store_context_pure.py
"""Phase 2: catalogue, dependency template, device resolution and rendering (no database)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.agentic.config import AgenticSettings
from app.agentic.store_context.assemble import assemble_snapshot
from app.agentic.store_context.render import estimate_tokens, render_snapshot
from app.agentic.store_context.resolve import resolve_device
from app.agentic.store_graph.catalogue import CatalogueError, load_catalogue
from app.agentic.store_graph.dependencies import (
    DependencyTemplateError,
    load_dependency_template,
)
from tests.agentic.seed_scenario import ROOT, SEED, seed_state

ANCHOR = datetime(2026, 10, 13, 3, 30, tzinfo=UTC)
CATALOGUE = load_catalogue(ROOT / "config" / "element_catalogue.yaml")
DEVICE_TYPES = ["pos", "register", "rf_gun", "printer", "workstation"]
ELEMENT_TYPES = ["wan_link", "gateway", "router", "switch", "access_point"]
GROUPS = ["Network Ops", "Retail EUC Ops", "Service Desk", "Command Centre"]


def snap(site: str = "01234"):
    settings = AgenticSettings(_env_file=None, environment="test")
    state = seed_state(site, ANCHOR, CATALOGUE)
    return assemble_snapshot(state, CATALOGUE, now=ANCHOR, settings=settings)


# --- catalogue -------------------------------------------------------------------------


def test_catalogue_matches_the_seeded_master_data() -> None:
    assert (
        CATALOGUE.problems_against(
            device_types=DEVICE_TYPES, element_types=ELEMENT_TYPES, assignment_groups=GROUPS
        )
        == []
    )


def test_catalogue_reports_differences() -> None:
    problems = CATALOGUE.problems_against(
        device_types=["pos", "kiosk"],
        element_types=ELEMENT_TYPES,
        assignment_groups=["Network Ops"],
    )
    text = "\n".join(problems)
    assert "'kiosk' is missing" in text
    assert "'register' does not exist in the database" in text
    assert "'Retail EUC Ops' is unknown" in text


@pytest.mark.parametrize(
    ("type_code", "reason", "fault", "staff"),
    [
        ("pos", "POS application error - terminal hardware not responding", "HW_FAULT", False),
        ("register", "Register application error - device not reachable", "NOT_REACHABLE", True),
        ("printer", "Paper jam reported by printer", "PAPER_JAM", True),
        ("printer", "Printer powered off", "POWER_OFF", True),
        ("rf_gun", "Low battery", "BATTERY_LOW", True),
        ("switch", "anything", None, False),
        ("printer", None, None, False),
    ],
)
def test_fault_rules_cover_the_seeded_reasons(
    type_code: str, reason: str | None, fault: str | None, staff: bool
) -> None:
    result = CATALOGUE.classify(type_code, reason)
    assert (result.fault_code, result.staff_can_fix) == (fault, staff)


def test_bad_catalogue_file(tmp_path: Path) -> None:
    with pytest.raises(CatalogueError, match="not found"):
        load_catalogue(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 1\ntypes:\n  Bad-Code: {}\n", encoding="utf-8")
    with pytest.raises(CatalogueError):
        load_catalogue(bad)


# --- dependency template ------------------------------------------------------------------


def test_dependency_template_matches_the_inventory_template() -> None:
    template = load_dependency_template(
        SEED / "agent_dependencies.yaml", SEED / "inventory_template.yaml", CATALOGUE
    )
    assert template.element_name("wan_pri", "01234") == "WAN01234-PRI"
    assert [(e.from_key, e.to_key, e.preferred) for e in template.element_edges][:2] == [
        ("gw", "wan_pri", True),
        ("gw", "wan_bkp", False),
    ]


def test_dependency_template_rejects_unknown_keys(tmp_path: Path) -> None:
    bad = tmp_path / "deps.yaml"
    bad.write_text(
        "element_edges:\n  - {from: gw, to: nope}\n"
        "device_services:\n  - {device_type: pos, service: lunch}\n",
        encoding="utf-8",
    )
    with pytest.raises(DependencyTemplateError) as error:
        load_dependency_template(bad, SEED / "inventory_template.yaml", CATALOGUE)
    assert "'nope'" in str(error.value) and "'lunch'" in str(error.value)


# --- device resolution --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "best", "how"),
    [
        ("POS Terminal 03", "POS Terminal 03", "name"),
        ("pos 3 is frozen", "POS Terminal 03", "number"),
        ("checkout 3 pos", "POS Terminal 03", "location"),
        ("till 2 printer", "Printer 02", "number"),
        ("register 4", "Register 04", "number"),
        ("my AP01234N004", "AP01234N004", "name"),
    ],
)
def test_resolve_device_best_match(text: str, best: str, how: str) -> None:
    [first, *_] = resolve_device(text, snap().nodes, CATALOGUE, limit=3)
    assert (first.node.name, first.matched_on) == (best, how)


def test_resolve_device_ambiguous_and_none() -> None:
    printers = resolve_device("the printer", snap().nodes, CATALOGUE, limit=5)
    assert [c.node.name for c in printers] == ["Printer 01", "Printer 02", "Printer 03"]
    assert {c.confidence for c in printers} == {0.3}
    till = resolve_device("till 3", snap().nodes, CATALOGUE, limit=5)
    assert {c.node.name for c in till} == {"POS Terminal 03", "Register 03"}
    assert resolve_device("the coffee machine", snap().nodes, CATALOGUE, limit=3) == []
    assert len(resolve_device("wifi", snap().nodes, CATALOGUE, limit=3)) == 3


# --- rendering ----------------------------------------------------------------------------


def test_render_lists_problems_first_within_budget() -> None:
    text = render_snapshot(snap(), max_tokens=800, max_device_lines=8, max_incident_lines=5)
    lines = text.splitlines()
    assert lines[0].startswith("Store 01234 Chennai Anna Nagar | local Tue 09:00")
    assert lines[1] == "Findings:"
    assert "INC00001234" in text and "running on Backup ISP link WAN01234-BKP" in text
    assert "Staff can fix." in text  # Register 04
    assert estimate_tokens(text) <= 800


def test_render_trims_but_keeps_major_findings() -> None:
    full = render_snapshot(snap(), max_tokens=800, max_device_lines=8, max_incident_lines=5)
    tight = render_snapshot(snap(), max_tokens=250, max_device_lines=8, max_incident_lines=5)
    assert len(tight) < len(full)
    for name in ("WAN01234-PRI", "POS Terminal 03", "Register 04"):
        assert name in tight
    assert "+1 more minor findings" in tight


def test_render_healthy_store() -> None:
    text = render_snapshot(snap("02112"), max_tokens=800, max_device_lines=8, max_incident_lines=5)
    assert "Findings: none - everything reports normally" in text
    assert "Incidents: none active" in text
