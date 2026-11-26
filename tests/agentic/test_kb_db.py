# tests/agentic/test_kb_db.py
"""Agentic Phase 3 against the test database: publishing, retrieval and guide state.

Every test runs in conftest's db_session (one transaction, always rolled back), so
nothing is left in ric_poc_test and no existing fixture changes.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.kb.articles import build_chunks, load_articles
from app.agentic.kb.embeddings import FakeEmbedder
from app.agentic.kb.guide import (
    GuideContext,
    GuideError,
    GuideView,
    get_guide,
    record_outcome,
    start_guide,
)
from app.agentic.kb.ingest import publish_articles
from app.agentic.kb.retrieval import find_articles
from app.agentic.values import ErrorCode, GuideStatus
from app.models import AgentChatSession, AgentKbArticle, AgentKbChunk, Store
from tests.agentic.test_kb_articles import ARTICLES, CATALOGUE

ARTICLE_LIST, _ = load_articles(ARTICLES)
CHUNKS = sum(len(build_chunks(a, CATALOGUE)) for a in ARTICLE_LIST)
MAX_CLARIFICATIONS = 2
ABANDON_MINUTES = 30


@dataclass
class KbWorld:
    session: AsyncSession
    ctx: GuideContext
    now: datetime


async def _store_and_chat(session: AsyncSession, user: str = "kb.test") -> GuideContext:
    store_id = uuid.uuid4()
    await session.execute(
        insert(Store).values(
            id=store_id,
            site_code="09999",
            name="KB Test Store",
            region="Test Region",
            country_code="IN",
            city="Test City",
            timezone="Asia/Kolkata",
            business_hours={},
        )
    )
    return await _chat(session, store_id, user)


async def _chat(session: AsyncSession, store_id: uuid.UUID, user: str) -> GuideContext:
    chat_id = uuid.uuid4()
    await session.execute(
        insert(AgentChatSession).values(
            id=chat_id, store_id=store_id, user_id=user, role="store_staff"
        )
    )
    return GuideContext(store_id=store_id, user_id=user, chat_session_id=chat_id)


@pytest.fixture
async def kb(db_session: AsyncSession) -> AsyncIterator[KbWorld]:
    now = datetime.now(UTC)
    await publish_articles(db_session, ARTICLE_LIST, CATALOGUE, embedder=FakeEmbedder(), now=now)
    yield KbWorld(db_session, await _store_and_chat(db_session), now)


async def _move(
    world: KbWorld,
    guide_id: str,
    step: str,
    outcome: str,
    version: int,
    *,
    reply: str | None = None,
) -> GuideView:
    return await record_outcome(
        world.session,
        world.ctx,
        uuid.UUID(guide_id),
        step_id=step,
        outcome=outcome,
        state_version=version,
        now=world.now,
        max_clarifications=MAX_CLARIFICATIONS,
        abandon_minutes=ABANDON_MINUTES,
        reply=reply,
    )


# --- publishing ---------------------------------------------------------------------------


async def test_publish_is_idempotent_and_versions_changes(db_session: AsyncSession) -> None:
    fake = FakeEmbedder()
    now = datetime.now(UTC)
    first = await publish_articles(db_session, ARTICLE_LIST, CATALOGUE, embedder=fake, now=now)
    assert len(first.added) == 7 and first.chunks_written == CHUNKS
    assert first.vectors_computed == CHUNKS and first.embedding_model == fake.model_key

    again = await publish_articles(db_session, ARTICLE_LIST, CATALOGUE, embedder=fake, now=now)
    assert len(again.unchanged) == 7 and again.chunks_written == 0

    changed = [
        a.model_copy(update={"summary": f"{a.summary} Updated."}) if a.kb_ref == "KB-PRN-002" else a
        for a in ARTICLE_LIST
    ]
    third = await publish_articles(db_session, changed, CATALOGUE, embedder=fake, now=now)
    assert third.changed == ["KB-PRN-002 v2"] and third.vectors_reused > 0
    rows = (
        await db_session.execute(
            select(AgentKbArticle.version, AgentKbArticle.status)
            .where(AgentKbArticle.kb_ref == "KB-PRN-002")
            .order_by(AgentKbArticle.version)
        )
    ).all()
    assert [(r.version, r.status) for r in rows] == [(1, "retired"), (2, "published")]
    chunks = await db_session.scalar(select(func.count()).select_from(AgentKbChunk))
    assert chunks == CHUNKS  # the retired version's chunks are gone

    removed = await publish_articles(db_session, changed[1:], CATALOGUE, embedder=fake, now=now)
    assert removed.retired == [ARTICLE_LIST[0].kb_ref]
    re_embedded = await publish_articles(
        db_session, changed[1:], CATALOGUE, embedder=fake, now=now, reembed=True
    )
    assert len(re_embedded.reembedded) == 6


async def test_publish_without_embeddings_stores_no_vectors(db_session: AsyncSession) -> None:
    now = datetime.now(UTC)
    report = await publish_articles(db_session, ARTICLE_LIST, CATALOGUE, embedder=None, now=now)
    assert report.embedding_model is None and report.vectors_computed == 0
    embedded = await db_session.scalar(
        select(func.count())
        .select_from(AgentKbChunk)
        .where(AgentKbChunk.embedding_model.is_not(None))
    )
    assert embedded == 0


# --- retrieval ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("element_type", "fault", "expected"),
    [
        ("printer", "PAPER_JAM", ["KB-PRN-001"]),
        ("printer", "POWER_OFF", ["KB-PRN-003"]),
        ("register", "NOT_REACHABLE", ["KB-NET-001"]),
        ("register", "APP_ERROR", ["KB-POS-001"]),
        ("rf_gun", "BATTERY_LOW", ["KB-RF-001"]),
        (None, "NOT_REACHABLE", ["KB-NET-001"]),
    ],
)
async def test_exact_match(
    kb: KbWorld, element_type: str | None, fault: str, expected: list[str]
) -> None:
    result = await find_articles(kb.session, element_type=element_type, fault_code=fault)
    assert [c.kb_ref for c in result.candidates] == expected
    assert {(c.matched_by, c.score) for c in result.candidates} == {("exact", 1.0)}


async def test_tags_then_nothing(kb: KbWorld) -> None:
    result = await find_articles(
        kb.session, element_type="rf_gun", fault_code="UNKNOWN", kb_tags=["rf-wifi"]
    )
    assert [(c.kb_ref, c.matched_by) for c in result.candidates] == [("KB-RF-002", "tag")]
    nothing = await find_articles(kb.session, element_type="pos", fault_code="PAPER_JAM")
    assert nothing.candidates == []


async def test_full_text_search(kb: KbWorld) -> None:
    result = await find_articles(kb.session, query="something is jammed")
    assert [c.kb_ref for c in result.candidates] == ["KB-PRN-001"]
    assert result.candidates[0].matched_by == "search"
    wrong_type = await find_articles(kb.session, query="something is jammed", element_type="pos")
    assert wrong_type.candidates == []


async def test_hybrid_search_with_vectors(kb: KbWorld) -> None:
    result = await find_articles(
        kb.session, query="printer paper jam stuck", embedder=FakeEmbedder(), min_similarity=0.05
    )
    assert result.candidates[0].kb_ref == "KB-PRN-001"
    assert result.warnings == []


# --- guide state ----------------------------------------------------------------------------


async def test_guide_walk_to_a_technician_step(kb: KbWorld) -> None:
    view = await start_guide(kb.session, kb.ctx, kb_ref="KB-PRN-001", now=kb.now)
    assert view.status == GuideStatus.IN_PROGRESS
    assert (view.step.step_id, view.step.step_no, view.state_version) == ("S1", 1, 1)
    assert [o.key for o in view.step.options][-2:] == ["unclear", "user_cannot_do"]
    view = await _move(kb, view.guide_id, "S1", "jam_seen", 1, reply="mail me at a.b@tcs.com")
    assert (view.step.step_id, view.step.step_no, view.state_version) == ("S2", 2, 2)
    view = await _move(kb, view.guide_id, "S2", "not_works", 2)
    view = await _move(kb, view.guide_id, "S3", "not_works", 3)
    assert view.status == GuideStatus.NEEDS_TECHNICIAN and view.step is None
    assert [t.step_id for t in view.steps_tried] == ["S1", "S2", "S3"]
    assert view.steps_tried[0].outcome_label == "Yes, paper is stuck"


async def test_guide_resolves_and_then_refuses_moves(kb: KbWorld) -> None:
    view = await start_guide(kb.session, kb.ctx, kb_ref="KB-NET-001", now=kb.now)
    view = await _move(kb, view.guide_id, "S1", "cables_fine", 1)
    view = await _move(kb, view.guide_id, "S3", "works", 2)
    assert view.status == GuideStatus.RESOLVED
    with pytest.raises(GuideError) as error:
        await _move(kb, view.guide_id, "S3", "works", 3)
    assert error.value.code == ErrorCode.CONFLICT and "ended" in error.value.message


async def test_guide_rejects_bad_moves(kb: KbWorld) -> None:
    view = await start_guide(kb.session, kb.ctx, kb_ref="KB-PRN-002", now=kb.now)
    for step, outcome, version, code in (
        ("S1", "no_paper", 7, ErrorCode.CONFLICT),
        ("S2", "works", 1, ErrorCode.CONFLICT),
        ("S1", "teleport", 1, ErrorCode.INVALID_INPUT),
    ):
        with pytest.raises(GuideError) as error:
            await _move(kb, view.guide_id, step, outcome, version)
        assert error.value.code == code
        assert error.value.current.step.step_id == "S1"
    other = await _chat(kb.session, kb.ctx.store_id, "someone.else")
    with pytest.raises(GuideError) as error:
        await get_guide(
            kb.session, other, uuid.UUID(view.guide_id), now=kb.now, abandon_minutes=ABANDON_MINUTES
        )
    assert error.value.code == ErrorCode.NOT_FOUND


async def test_unclear_then_gives_up(kb: KbWorld) -> None:
    view = await start_guide(kb.session, kb.ctx, kb_ref="KB-RF-002", now=kb.now)
    view = await _move(kb, view.guide_id, "S1", "unclear", 1)
    view = await _move(kb, view.guide_id, "S1", "unclear", 2)
    assert (view.step.step_id, view.clarifications) == ("S1", 2)
    view = await _move(kb, view.guide_id, "S1", "unclear", 3)
    assert view.status == GuideStatus.NEEDS_TECHNICIAN
    assert [t.outcome for t in view.steps_tried] == ["user_cannot_do"]


async def test_one_guide_per_chat_and_idle_abandon(kb: KbWorld) -> None:
    first = await start_guide(kb.session, kb.ctx, kb_ref="KB-PRN-002", now=kb.now)
    second = await start_guide(kb.session, kb.ctx, kb_ref="KB-PRN-003", now=kb.now)
    old = await get_guide(
        kb.session, kb.ctx, uuid.UUID(first.guide_id), now=kb.now, abandon_minutes=ABANDON_MINUTES
    )
    assert old.status == GuideStatus.ABANDONED
    later = kb.now + timedelta(minutes=ABANDON_MINUTES + 2)
    idle = await get_guide(
        kb.session, kb.ctx, uuid.UUID(second.guide_id), now=later, abandon_minutes=ABANDON_MINUTES
    )
    assert idle.status == GuideStatus.ABANDONED


async def test_unknown_article(kb: KbWorld) -> None:
    with pytest.raises(GuideError) as error:
        await start_guide(kb.session, kb.ctx, kb_ref="KB-XXX-999", now=kb.now)
    assert error.value.code == ErrorCode.NOT_FOUND
