# tests/agentic/test_store_snapshot_db.py
"""Phase 2 against the seeded test database (conftest itsm_engine: every seed step,
anchored at ITSM_ANCHOR). Proves the graph, the seed and the app screens agree:
the same expectations as the pure scenario test, read through the real queries.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.config import AgenticSettings
from app.agentic.store_context.assemble import StoreSnapshot
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_context.render import estimate_tokens, render_snapshot
from app.agentic.store_context.snapshot import begin_read_only, build_snapshot
from app.agentic.store_graph.dependencies import load_dependency_template
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.errors import NotFoundError
from app.db.session import create_session_factory
from app.models import AgentStoreDependency, AssignmentGroup, DeviceType, NetworkElementType
from tests.agentic.seed_scenario import SEED
from tests.agentic.test_store_scenarios import CATALOGUE, EXPECTED
from tests.conftest import ITSM_ANCHOR

SETTINGS = AgenticSettings(_env_file=None, environment="test")
TEMPLATE = load_dependency_template(
    SEED / "agent_dependencies.yaml", SEED / "inventory_template.yaml", CATALOGUE
)
EDGES_PER_STORE = 12  # 4 element edges + 4 POS + 4 registers using card payments


async def _seed_edges(engine: AsyncEngine) -> dict[str, int]:
    async with AsyncSession(engine, expire_on_commit=False) as session:
        report = await write_store_edges(session, TEMPLATE)
        await session.commit()
    return report.edges


def _actual(snapshot: StoreSnapshot) -> dict[str, tuple[Any, ...]]:
    return {
        f.root_node.name: (
            f.severity,
            f.status.value,
            f.fault_code,
            f.staff_can_fix,
            f.covered_by_incident,
            [n.name for n in f.impacted_nodes],
        )
        for f in snapshot.findings
        if f.kind == "root_cause"
    }


async def test_catalogue_matches_the_seeded_database(itsm_engine: AsyncEngine) -> None:
    async with AsyncSession(itsm_engine) as session:
        problems = CATALOGUE.problems_against(
            device_types=(await session.scalars(select(DeviceType.code))).all(),
            element_types=(await session.scalars(select(NetworkElementType.code))).all(),
            assignment_groups=(await session.scalars(select(AssignmentGroup.name))).all(),
        )
    assert problems == []


async def test_edges_are_written_for_every_store_and_idempotent(itsm_engine: AsyncEngine) -> None:
    first = await _seed_edges(itsm_engine)
    second = await _seed_edges(itsm_engine)
    assert len(first) == 10 and set(first.values()) == {EDGES_PER_STORE}
    assert first == second
    async with AsyncSession(itsm_engine) as session:
        total = await session.scalar(select(func.count()).select_from(AgentStoreDependency))
    assert total == 10 * EDGES_PER_STORE


async def test_every_seeded_store_matches_the_scenarios(itsm_engine: AsyncEngine) -> None:
    await _seed_edges(itsm_engine)
    factory = create_session_factory(itsm_engine)
    async with factory() as session:
        sites = (await session.scalars(text("SELECT site_code FROM stores ORDER BY 1"))).all()
    assert len(sites) == 10
    for site in sites:
        async with factory() as session:
            store_id = await store_id_for_site(session, site)
        snapshot = await build_snapshot(
            factory, store_id, now=ITSM_ANCHOR, catalogue=CATALOGUE, settings=SETTINGS
        )
        assert snapshot.warnings == [], site
        assert _actual(snapshot) == EXPECTED.get(site, {}), site


async def test_chennai_snapshot_text(itsm_engine: AsyncEngine) -> None:
    await _seed_edges(itsm_engine)
    factory = create_session_factory(itsm_engine)
    async with factory() as session:
        store_id = await store_id_for_site(session, "01234")
    snapshot = await build_snapshot(
        factory, store_id, now=ITSM_ANCHOR, catalogue=CATALOGUE, settings=SETTINGS
    )
    assert {i.number for i in snapshot.incidents} == {
        "INC00001234",
        "INC00001235",
        "INC00001236",
        "INC00001237",
    }
    assert [i.number for i in snapshot.recently_resolved] == ["INC00001238"]
    assert snapshot.visits, "the seeded Chennai appointments are in the next 48 hours"
    text_out = render_snapshot(snapshot, max_tokens=800, max_device_lines=8, max_incident_lines=5)
    assert text_out.startswith("Store 01234 Chennai Anna Nagar")
    assert "running on Backup ISP link WAN01234-BKP" in text_out
    assert estimate_tokens(text_out) <= 800


async def test_snapshot_transaction_is_repeatable_read_and_read_only(
    itsm_engine: AsyncEngine,
) -> None:
    factory = create_session_factory(itsm_engine)
    async with factory() as session:
        await begin_read_only(session, 5)
        assert await session.scalar(text("SHOW transaction_isolation")) == "repeatable read"
        assert await session.scalar(text("SHOW transaction_read_only")) == "on"
        assert await session.scalar(text("SHOW statement_timeout")) == "5s"
        await session.rollback()


async def test_unknown_store_is_not_found(itsm_engine: AsyncEngine) -> None:
    with pytest.raises(NotFoundError):
        await build_snapshot(
            create_session_factory(itsm_engine),
            uuid.uuid4(),
            now=ITSM_ANCHOR,
            catalogue=CATALOGUE,
            settings=SETTINGS,
        )
