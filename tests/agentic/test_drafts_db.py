# tests/agentic/test_drafts_db.py
"""Agentic Phase 4 against the seeded test database (conftest itsm_engine, anchored
at ITSM_ANCHOR). Everything runs in one session per test that is rolled back, so
no draft, chat, guide or incident is left behind and no existing fixture changes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.drafts.mapping import draft_to_new_incident, store_manager_actor
from app.agentic.drafts.service import (
    DraftContext,
    DraftError,
    DraftProposal,
    DraftRevision,
    PrepareResult,
    cancel_draft,
    fresh_snapshot,
    get_draft,
    prepare_draft,
    revise_draft,
)
from app.agentic.kb.articles import load_articles
from app.agentic.kb.guide import GuideContext, record_outcome, start_guide
from app.agentic.kb.ingest import publish_articles
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.agentic.values import AgentRole, DraftStatus, ErrorCode
from app.models import AgentChatSession, AgentIncidentDraft
from app.services import incidents as incident_service
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import SETTINGS, TEMPLATE
from tests.conftest import ITSM_ANCHOR

NOW = ITSM_ANCHOR
MANAGER = "priya.store-manager"


@pytest.fixture
async def session(itsm_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(itsm_engine, expire_on_commit=False) as db:
        await write_store_edges(db, TEMPLATE)
        try:
            yield db
        finally:
            await db.rollback()


async def _ctx(
    db: AsyncSession, site: str, user: str = MANAGER, role: AgentRole = AgentRole.STORE_MANAGER
) -> DraftContext:
    store_id = await store_id_for_site(db, site)
    chat_id = uuid.uuid4()
    await db.execute(
        insert(AgentChatSession).values(id=chat_id, store_id=store_id, user_id=user, role=role)
    )
    return DraftContext(store_id=store_id, user_id=user, chat_session_id=chat_id, role=role)


async def _finding(db: AsyncSession, ctx: DraftContext, root: str) -> str:
    snapshot = await fresh_snapshot(
        db, ctx.store_id, now=NOW, catalogue=CATALOGUE, settings=SETTINGS
    )
    return next(f.finding_id for f in snapshot.findings if f.root_node.name == root)


def _proposal(**values: Any) -> DraftProposal:
    base: dict[str, Any] = {
        "short_description": "RF Gun 02 - battery does not charge",
        "description": "RF Gun 02 shows low battery and does not charge in its cradle.",
        "u_symptom": "my scanner keeps dying",
        "impact": 3,
        "urgency": 3,
        "reasons": {"impact": "one handheld", "urgency": "spares available"},
    }
    return DraftProposal(**{**base, **values})


async def _prepare(db: AsyncSession, ctx: DraftContext, **values: Any) -> PrepareResult:
    return await prepare_draft(
        db, ctx, _proposal(**values), now=NOW, catalogue=CATALOGUE, settings=SETTINGS
    )


async def test_ready_draft_with_clamped_priority_and_evidence(session: AsyncSession) -> None:
    ctx = await _ctx(session, "03340")
    finding = await _finding(session, ctx, "RF Gun 02")
    result = await _prepare(session, ctx, finding_id=finding, impact=1, urgency=1)
    assert result.outcome == "draft_ready" and result.duplicates.result == "none"
    draft = result.draft
    assert (draft.baseline_impact, draft.baseline_urgency) == (3, 3)
    assert (draft.impact, draft.urgency, draft.priority) == (2, 2, 3)
    assert "impact_adjusted" in draft.reasons and "urgency_adjusted" in draft.reasons
    assert (draft.category, draft.subcategory, draft.assignment_group) == (
        "hardware",
        "RF Gun",
        "Retail EUC Ops",
    )
    assert draft.affected.name == "RF Gun 02" and draft.finding_ids == [finding]
    assert "--- RIC evidence (generated) ---" in draft.description
    assert "BATTERY_LOW" in draft.description and "03340" in draft.description
    assert draft.expires_at == NOW + timedelta(minutes=SETTINGS.incident_draft_ttl_minutes)
    row = await session.get(AgentIncidentDraft, uuid.UUID(draft.draft_id))
    assert row.cmdb_ci_device_id is not None and row.cmdb_ci_network_element_id is None


async def test_strong_duplicates_write_no_draft(session: AsyncSession) -> None:
    for site, root, number in (
        ("01234", "WAN01234-PRI", "INC00001234"),
        ("01234", "POS Terminal 03", "INC00001235"),
        ("02556", "SW02556N303", "INC00001303"),
    ):
        ctx = await _ctx(session, site, user=f"m-{root}")
        result = await _prepare(
            session, ctx, finding_id=await _finding(session, ctx, root),
            short_description=f"{root} down", description=f"{root} is down.",
        )  # fmt: skip
        assert result.outcome == "duplicate_found" and result.draft is None
        assert number in [m.incident_number for m in result.duplicates.matches]
    count = await session.scalar(select(func.count()).select_from(AgentIncidentDraft))
    assert count == 0


async def test_text_must_stay_within_the_evidence(session: AsyncSession) -> None:
    ctx = await _ctx(session, "03340")
    finding = await _finding(session, ctx, "RF Gun 02")
    for description, bad in (
        ("RF Gun 02 and Printer 01 are both broken.", "Printer 01"),
        ("Same as INC00001234.", "INC00001234"),
    ):
        with pytest.raises(DraftError) as error:
            await _prepare(session, ctx, finding_id=finding, description=description)
        assert error.value.code == ErrorCode.INVALID_INPUT
        assert error.value.details["not_in_evidence"] == [bad]
    with pytest.raises(DraftError, match="subcategory"):
        await _prepare(session, ctx, finding_id=finding, subcategory="Printer")
    ok = await _prepare(
        session, ctx, finding_id=finding, description="Call 98765 43210. RF Gun 02 is dead."
    )
    assert ok.draft.description.startswith("Call [number]. RF Gun 02 is dead.")


async def test_role_cleared_finding_and_rate_limit(session: AsyncSession) -> None:
    staff = await _ctx(session, "03340", user="ravi", role=AgentRole.STORE_STAFF)
    with pytest.raises(DraftError) as error:
        await _prepare(session, staff)
    assert error.value.code == ErrorCode.FORBIDDEN
    ctx = await _ctx(session, "03340")
    with pytest.raises(DraftError) as error:
        await _prepare(session, ctx, finding_id="F-00000000")
    assert error.value.code == ErrorCode.CONFLICT
    tight = SETTINGS.model_copy(update={"mcp_max_drafts_per_session_per_hour": 1})
    finding = await _finding(session, ctx, "RF Gun 02")
    await prepare_draft(
        session, ctx, _proposal(finding_id=finding), now=NOW, catalogue=CATALOGUE, settings=tight
    )
    with pytest.raises(DraftError) as error:
        await prepare_draft(
            session, ctx, _proposal(finding_id=finding), now=NOW, catalogue=CATALOGUE,
            settings=tight,
        )  # fmt: skip
    assert error.value.code == ErrorCode.RATE_LIMITED


async def test_supersede_revise_expire_cancel(session: AsyncSession) -> None:
    ctx = await _ctx(session, "03340")
    finding = await _finding(session, ctx, "RF Gun 02")
    first = (await _prepare(session, ctx, finding_id=finding)).draft
    second = (await _prepare(session, ctx, finding_id=finding)).draft
    old = await get_draft(session, ctx, uuid.UUID(first.draft_id), now=NOW)
    assert old.status == DraftStatus.SUPERSEDED

    draft_id = uuid.UUID(second.draft_id)
    with pytest.raises(DraftError) as error:
        await revise_draft(
            session, ctx, draft_id, DraftRevision(version=9), now=NOW, catalogue=CATALOGUE,
            settings=SETTINGS,
        )  # fmt: skip
    assert error.value.code == ErrorCode.CONFLICT
    revised = await revise_draft(
        session, ctx, draft_id,
        DraftRevision(version=1, impact=1, description="RF Gun 02 will not charge at all."),
        now=NOW, catalogue=CATALOGUE, settings=SETTINGS,
    )  # fmt: skip
    assert (revised.version, revised.impact) == (2, 2)
    assert revised.description.startswith("RF Gun 02 will not charge at all.")
    assert "--- RIC evidence (generated) ---" in revised.description

    later = NOW + timedelta(minutes=SETTINGS.incident_draft_ttl_minutes)
    assert (await get_draft(session, ctx, draft_id, now=later)).status == DraftStatus.EXPIRED
    with pytest.raises(DraftError) as error:
        await cancel_draft(session, ctx, draft_id, now=later)
    assert error.value.code == ErrorCode.CONFLICT

    third = (await _prepare(session, ctx, finding_id=finding)).draft
    cancelled = await cancel_draft(session, ctx, uuid.UUID(third.draft_id), now=NOW)
    assert cancelled.status == DraftStatus.CANCELLED


async def test_possible_duplicate_and_store_level_draft(session: AsyncSession) -> None:
    first = await _ctx(session, "03340")
    finding = await _finding(session, first, "RF Gun 02")
    await _prepare(session, first, finding_id=finding)
    other = await _ctx(session, "03340", user="other.manager")
    result = await _prepare(session, other, finding_id=finding)
    assert result.outcome == "possible_duplicate" and result.draft is None
    acknowledged = await _prepare(
        session, other, finding_id=finding, acknowledge_possible_duplicates=True
    )
    assert acknowledged.outcome == "draft_ready"

    store = await _ctx(session, "02112", user="store.level")
    with pytest.raises(DraftError, match="category must be one of"):
        await _prepare(
            session, store, short_description="Music system silent",
            description="The in-store music has stopped.",
        )  # fmt: skip
    level = await _prepare(
        session, store, short_description="Music system silent",
        description="The in-store music has stopped.", category="inquiry", impact=2,
    )  # fmt: skip
    draft = level.draft
    assert (draft.category, draft.assignment_group, draft.affected) == (
        "inquiry",
        "Service Desk",
        None,
    )
    assert (draft.baseline_impact, draft.impact) == (3, 2)


async def test_guide_handoff_and_incident_fields(session: AsyncSession) -> None:
    articles, _ = load_articles(ARTICLES)
    await publish_articles(session, articles, CATALOGUE, embedder=None, now=NOW)
    ctx = await _ctx(session, "03340")
    snapshot = await fresh_snapshot(
        session, ctx.store_id, now=NOW, catalogue=CATALOGUE, settings=SETTINGS
    )
    gun = next(f for f in snapshot.findings if f.root_node.name == "RF Gun 02")
    guide_ctx = GuideContext(ctx.store_id, ctx.user_id, ctx.chat_session_id)
    view = await start_guide(
        session, guide_ctx, kb_ref="KB-RF-001", now=NOW,
        subject_device_id=uuid.UUID(gun.root_node.id), finding_ids=[gun.finding_id],
    )  # fmt: skip
    for step, version in (("S1", 1), ("S3", 2)):
        view = await record_outcome(
            session, guide_ctx, uuid.UUID(view.guide_id), step_id=step, outcome="not_charging",
            state_version=version, now=NOW, max_clarifications=2, abandon_minutes=30,
        )  # fmt: skip
    assert view.status == "needs_technician"

    result = await _prepare(
        session, ctx, finding_id=gun.finding_id, guide_id=uuid.UUID(view.guide_id)
    )
    draft = result.draft
    assert draft.baseline_urgency == 2  # the guide ended needing a technician
    assert [s["step_id"] for s in draft.steps_tried] == ["S1", "S3"]
    assert "Steps already tried (KB-RF-001 v1)" in draft.description

    row = await session.get(AgentIncidentDraft, uuid.UUID(draft.draft_id))
    data = draft_to_new_incident(row)
    incident = await incident_service.create_incident(
        session, store_manager_actor(ctx.user_id), ctx.store_id, data, NOW, commit=False
    )
    assert (incident.category, incident.subcategory, incident.contact_type) == (
        "hardware",
        "RF Gun",
        "self_service",
    )
    assert incident.cmdb_ci_device_id == row.cmdb_ci_device_id
    assert (incident.impact, incident.urgency, incident.caller) == (
        row.impact,
        row.urgency,
        MANAGER,
    )
