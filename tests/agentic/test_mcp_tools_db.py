# tests/agentic/test_mcp_tools_db.py
"""Agentic Phase 5: every tool function on the seeded test database (conftest
itsm_engine at ITSM_ANCHOR), called directly in one session that is rolled back."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.common.tokens import ToolIdentity
from app.agentic.kb.articles import load_articles
from app.agentic.kb.ingest import publish_articles
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.agentic.tools import itsm, kb, store
from app.agentic.tools.base import ToolContext, ToolFailure
from app.agentic.values import AgentRole, ErrorCode, SpecialistName
from app.models import AgentChatSession
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import SETTINGS, TEMPLATE
from tests.conftest import ITSM_ANCHOR

MANAGER = "priya.store-manager"


@pytest.fixture
async def session(itsm_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(itsm_engine, expire_on_commit=False) as db:
        await write_store_edges(db, TEMPLATE)
        articles, _ = load_articles(ARTICLES)
        await publish_articles(db, articles, CATALOGUE, embedder=None, now=ITSM_ANCHOR)
        try:
            yield db
        finally:
            await db.rollback()


async def _ctx(
    db: AsyncSession,
    site: str,
    agent: str = "store_insight",
    role: str = "store_manager",
    chat: bool = False,
    user: str = MANAGER,
) -> ToolContext:
    store_id = await store_id_for_site(db, site)
    chat_id = None
    if chat:
        chat_id = uuid.uuid4()
        await db.execute(
            insert(AgentChatSession).values(id=chat_id, store_id=store_id, user_id=user, role=role)
        )
    identity = ToolIdentity(
        user_id=user,
        role=AgentRole(role),
        store_id=store_id,
        agent=SpecialistName(agent),
        correlation_id=uuid.uuid4(),
        policy_version="1",
        chat_session_id=chat_id,
    )
    return ToolContext(identity, ITSM_ANCHOR, SETTINGS, CATALOGUE)


def _names(items: list[dict[str, Any]]) -> set[str]:
    return {item["node"]["name"] for item in items}


async def _node_id(db: AsyncSession, ctx: ToolContext, name: str) -> str:
    out = await store.store_resolve_device(db, ctx, store.ResolveArgs(text=name))
    return out.data["candidates"][0]["node"]["id"]


async def test_findings_and_resolution(session: AsyncSession) -> None:
    ctx = await _ctx(session, "02556")
    out = await store.store_get_findings(session, ctx, store.FindingsArgs())
    [finding] = out.data["findings"]
    assert finding["root_node"]["name"] == "SW02556N303" and finding["severity"] == "critical"
    assert len(finding["impacted_nodes"]) == 5 and out.data["store"]["site_code"] == "02556"
    none = await store.store_get_findings(
        session, ctx, store.FindingsArgs(severity_min="critical", include_suspects=False)
    )
    assert len(none.data["findings"]) == 1

    gun = await _ctx(session, "03340")
    resolved = await store.store_resolve_device(session, gun, store.ResolveArgs(text="rf gun 2"))
    assert resolved.data["candidates"][0]["node"]["name"] == "RF Gun 02"


async def test_node_status_and_history(session: AsyncSession) -> None:
    ctx = await _ctx(session, "03340")
    gun = await _node_id(session, ctx, "RF Gun 02")
    status = await store.store_get_node_status(session, ctx, store.NodeArgs(node_id=gun))
    assert status.data["reported"] == "degraded" and status.data["reason"] == "Low battery"
    device = await store.store_get_node_history(session, ctx, store.HistoryArgs(node_id=gun))
    assert device.data["events"] == [] and "current status" in device.data["note"]

    flappy = await _ctx(session, "03118")
    ap = await _node_id(session, flappy, "AP03118N002")
    history = await store.store_get_node_history(
        session, flappy, store.HistoryArgs(node_id=ap, hours=12)
    )
    assert history.data["flap_count"] >= 4 and history.data["flapping"]

    with pytest.raises(ToolFailure) as error:
        await store.store_get_node_status(session, ctx, store.NodeArgs(node_id=ap))
    assert error.value.code == ErrorCode.NOT_FOUND  # another store's node


async def test_graph_tools(session: AsyncSession) -> None:
    paris = await _ctx(session, "02556")
    switch = await _node_id(session, paris, "SW02556N303")
    around = await store.graph_neighbourhood(
        session, paris, store.NeighbourhoodArgs(node_id=switch)
    )
    assert "SW02556N301" in _names(around.data["depends_on"])
    assert {"AP02556N003", "Workstation 01"} <= _names(around.data["dependants"])
    impact = await store.graph_impact_of(session, paris, store.NodeArgs(node_id=switch))
    assert impact.data["impacted_count"] >= 5

    chennai = await _ctx(session, "01234")
    wan = await _node_id(session, chennai, "WAN01234-PRI")
    backup = await store.graph_impact_of(session, chennai, store.NodeArgs(node_id=wan))
    assert backup.data["impacted_count"] == 0
    assert "GW01234N001" in _names(backup.data["loses_backup"])
    pos = await _node_id(session, chennai, "POS Terminal 01")
    path = await store.graph_explain_path(
        session, chennai, store.PathArgs(from_node_id=pos, to_node_id=wan)
    )
    assert path.data["connected"] and path.data["direction"] == "depends_on"
    steps = [s["node"]["name"] for s in path.data["path"]]
    assert steps[0] == "POS Terminal 01" and steps[-1] == "WAN01234-PRI"


async def test_kb_tools_and_guide(session: AsyncSession) -> None:
    ctx = await _ctx(session, "01456", agent="kb_guide", role="store_staff", chat=True)
    found = await kb.kb_find_articles(
        session, ctx, kb.FindArgs(element_type="printer", fault_code="PAPER_JAM")
    )
    assert [c["kb_ref"] for c in found.data["candidates"]] == ["KB-PRN-001"]
    article = await kb.kb_get_article(session, ctx, kb.ArticleArgs(kb_ref="KB-PRN-001"))
    assert [s["step_id"] for s in article.data["outline"]] == ["S1", "S2", "S3"]
    printer = await _node_id(session, ctx, "Printer 02")
    started = await kb.kb_guide_start(
        session, ctx, kb.StartArgs(kb_ref="KB-PRN-001", subject_node_id=printer)
    )
    guide_id = started.data["guide_id"]
    assert started.data["step"]["step_id"] == "S1" and "step.instruction" in started.untrusted
    moved = await kb.kb_guide_record_outcome(
        session,
        ctx,
        kb.OutcomeArgs(guide_id=guide_id, step_id="S1", outcome="jam_seen", state_version=1),
    )
    assert moved.data["step"]["step_id"] == "S2"
    with pytest.raises(ToolFailure) as error:
        await kb.kb_guide_record_outcome(
            session,
            ctx,
            kb.OutcomeArgs(guide_id=guide_id, step_id="S1", outcome="jam_seen", state_version=1),
        )
    assert error.value.code == ErrorCode.CONFLICT and "kb_guide_get_state" in error.value.message
    ended = await kb.kb_guide_end(
        session, ctx, kb.EndArgs(guide_id=guide_id, reason="user_cannot_do")
    )
    assert ended.data["status"] == "needs_technician"

    no_chat = await _ctx(session, "01456", agent="kb_guide")
    with pytest.raises(ToolFailure) as error:
        await kb.kb_guide_start(session, no_chat, kb.StartArgs(kb_ref="KB-PRN-001"))
    assert error.value.code == ErrorCode.INVALID_INPUT


async def test_itsm_tools(session: AsyncSession) -> None:
    ctx = await _ctx(session, "01234", agent="incident_desk")
    active = await itsm.itsm_search_incidents(session, ctx, itsm.SearchArgs())
    assert len(active.data["incidents"]) == 4
    wan = await _node_id(session, ctx, "WAN01234-PRI")
    for_wan = await itsm.itsm_search_incidents(session, ctx, itsm.SearchArgs(node_id=wan))
    assert [i["number"] for i in for_wan.data["incidents"]] == ["INC00001234"]
    resolved = await itsm.itsm_search_incidents(
        session, ctx, itsm.SearchArgs(state="recent_resolved")
    )
    assert [i["number"] for i in resolved.data["incidents"]] == ["INC00001238"]
    detail = await itsm.itsm_get_incident(session, ctx, itsm.IncidentArgs(number="INC00001234"))
    assert detail.data["state_label"] and "public_updates[].text" in detail.untrusted

    other = await _ctx(session, "03340", agent="incident_desk", chat=True)
    with pytest.raises(ToolFailure) as error:
        await itsm.itsm_get_incident(session, other, itsm.IncidentArgs(number="INC00001234"))
    assert error.value.code == ErrorCode.NOT_FOUND
    findings = await store.store_get_findings(session, other, store.FindingsArgs())
    gun = next(f for f in findings.data["findings"] if f["root_node"]["name"] == "RF Gun 02")
    duplicates = await itsm.itsm_check_duplicates(
        session, other, itsm.DuplicateArgs(finding_id=gun["finding_id"], text="RF Gun 02 battery")
    )
    assert duplicates.data["result"] == "none"
    prepared = await itsm.itsm_prepare_draft(
        session,
        other,
        itsm.PrepareArgs(
            short_description="RF Gun 02 - battery does not charge",
            description="RF Gun 02 shows low battery and does not charge.",
            impact=3,
            urgency=3,
            finding_id=gun["finding_id"],
        ),
    )
    assert prepared.data["outcome"] == "draft_ready"
    draft = prepared.data["draft"]
    read = await itsm.itsm_get_draft(session, other, itsm.DraftArgs(draft_id=draft["draft_id"]))
    assert read.data["draft"]["version"] == 1
    revised = await itsm.itsm_revise_draft(
        session,
        other,
        itsm.ReviseArgs(draft_id=draft["draft_id"], version=1, urgency=2),
    )
    assert revised.data["draft"]["urgency"] == 2 and revised.data["draft"]["version"] == 2

    staff = await _ctx(
        session, "03340", agent="incident_desk", role="store_staff", chat=True, user="ravi"
    )
    with pytest.raises(ToolFailure) as error:
        await itsm.itsm_prepare_draft(
            session,
            staff,
            itsm.PrepareArgs(short_description="x", description="y", impact=3, urgency=3),
        )
    assert error.value.code == ErrorCode.FORBIDDEN
