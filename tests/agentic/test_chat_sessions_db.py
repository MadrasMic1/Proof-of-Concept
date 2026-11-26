# tests/agentic/test_chat_sessions_db.py
"""Agentic Phase 7: sessions, the turn lock, derived state and compaction on the seeded
TEST database (app login). Data is committed, so everything added is deleted at the end."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.chat.compaction import compact
from app.agentic.chat.sessions import (
    TurnLock,
    add_message,
    close_session,
    ensure_session,
    find_client_message,
    load_window,
)
from app.agentic.chat.state import derive_state
from app.agentic.config import AgenticSettings
from app.agentic.kb.articles import load_articles
from app.agentic.kb.guide import GuideContext, record_outcome, start_guide
from app.agentic.kb.ingest import publish_articles
from app.agentic.llm.client import ModelTarget, build_chat_client
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.values import ChatSessionStatus, ConversationState, MessageRole
from app.db.session import create_session_factory
from app.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentKbArticle,
    AgentKbChunk,
    AgentKbGuideSession,
    AgentLlmCall,
)
from tests.agentic.agent_script import ScriptedModel, text
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.conftest import ITSM_ANCHOR

SETTINGS = AgenticSettings(_env_file=None, environment="test")
NOW = ITSM_ANCHOR
TARGET = ModelTarget("fake", "scripted-model", "http://fake.local/v1", "fake", 30)


async def _cleanup(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        for model in (
            AgentLlmCall,
            AgentChatMessage,
            AgentKbGuideSession,
            AgentChatSession,
            AgentKbChunk,
            AgentKbArticle,
        ):
            await db.execute(delete(model))
        await db.commit()


async def test_sessions_lock_and_messages(itsm_engine: AsyncEngine) -> None:
    factory = create_session_factory(itsm_engine)
    try:
        async with factory() as db:
            store = await store_id_for_site(db, "03340")
        kw = {"store_id": store, "user_id": "ravi", "role": "store_staff", "idle_minutes": 30}
        first = await ensure_session(factory, now=NOW, **kw)
        assert await ensure_session(factory, now=NOW + timedelta(minutes=5), **kw) == first
        later = await ensure_session(factory, now=NOW + timedelta(minutes=45), **kw)
        assert later != first
        async with factory() as db:
            old = await db.get(AgentChatSession, first)
            assert old.status == ChatSessionStatus.EXPIRED and old.closed_at is not None
        await close_session(factory, later, NOW)
        current = await ensure_session(factory, now=NOW, **kw)
        assert current not in (first, later)

        lock = TurnLock(itsm_engine, current)
        other = TurnLock(itsm_engine, current)
        assert await lock.acquire()
        assert not await other.acquire()
        await lock.release()
        assert await other.acquire()
        await other.release()

        client = uuid.uuid4()
        async with factory() as db:
            await add_message(
                db, session_id=current, store_id=store, turn_no=1, role=MessageRole.USER,
                content="hi", correlation_id=uuid.uuid4(), now=NOW, client_message_id=client,
            )  # fmt: skip
            found = await find_client_message(db, current, client)
            assert found is not None and found.turn_no == 1 and found.response is None
            await add_message(
                db, session_id=current, store_id=store, turn_no=1, role=MessageRole.ASSISTANT,
                content="hello", correlation_id=uuid.uuid4(), now=NOW, response={"answer": "x"},
            )  # fmt: skip
            await db.commit()
            found = await find_client_message(db, current, client)
            assert found is not None and found.response == {"answer": "x"}
    finally:
        await _cleanup(itsm_engine)


async def test_derived_state_and_acknowledgement(itsm_engine: AsyncEngine) -> None:
    factory = create_session_factory(itsm_engine)
    try:
        async with factory() as db:
            articles, _ = load_articles(ARTICLES)
            await publish_articles(db, articles, CATALOGUE, embedder=None, now=NOW)
            await db.commit()
            store = await store_id_for_site(db, "03340")
        chat = await ensure_session(
            factory, store_id=store, user_id="ravi", role="store_staff", now=NOW, idle_minutes=30
        )
        ctx = GuideContext(store, "ravi", chat)
        async with factory() as db:
            assert (await derive_state(db, chat, {}, now=NOW, abandon_minutes=30)).state == (
                ConversationState.IDLE
            )
            view = await start_guide(db, ctx, kb_ref="KB-RF-001", now=NOW)
            await db.commit()
            guiding = await derive_state(db, chat, {}, now=NOW, abandon_minutes=30)
            assert guiding.state == ConversationState.GUIDING
            assert (guiding.guide.kb_ref, guiding.guide.current_step) == ("KB-RF-001", "S1")
            assert guiding.guide.ask == "Is the charging light on?"
            for step, version in (("S1", 1), ("S3", 2)):
                view = await record_outcome(
                    db, ctx, uuid.UUID(view.guide_id), step_id=step, outcome="not_charging",
                    state_version=version, now=NOW, max_clarifications=2, abandon_minutes=30,
                )  # fmt: skip
            await db.commit()
            ended = await derive_state(db, chat, {}, now=NOW, abandon_minutes=30)
            assert ended.state == ConversationState.GUIDE_EXHAUSTED
            acked = {"ack_guides": [view.guide_id]}
            idle = await derive_state(db, chat, acked, now=NOW, abandon_minutes=30)
            assert idle.state == ConversationState.IDLE
    finally:
        await _cleanup(itsm_engine)


async def test_window_and_compaction(itsm_engine: AsyncEngine) -> None:
    factory = create_session_factory(itsm_engine)
    try:
        async with factory() as db:
            store = await store_id_for_site(db, "03340")
        chat_id = await ensure_session(
            factory, store_id=store, user_id="ravi", role="store_staff", now=NOW, idle_minutes=30
        )
        async with factory() as db:
            for turn in range(1, 9):
                pair = ((MessageRole.USER, f"q{turn}"), (MessageRole.ASSISTANT, f"a{turn}"))
                for role, content in pair:
                    await add_message(
                        db, session_id=chat_id, store_id=store, turn_no=turn, role=role,
                        content=content, correlation_id=uuid.uuid4(), now=NOW,
                        response={"answer": content} if role == MessageRole.ASSISTANT else None,
                    )  # fmt: skip
            chat = await db.get(AgentChatSession, chat_id)
            chat.turn_count = 8
            await db.commit()
            window = await load_window(db, chat, SETTINGS, 9)
            assert [(w.turn_no, w.role) for w in window][:2] == [(1, "user"), (1, "assistant")]

        model = ScriptedModel([text("User asked q1-q2 about RF Gun 02.")])

        def client_for(target: ModelTarget) -> object:
            return build_chat_client(target, SETTINGS, "summariser", inner=model.transport())

        done = await compact(
            factory, chat_id, settings=SETTINGS, target=TARGET, client_for=client_for
        )
        assert done
        async with factory() as db:
            chat = await db.get(AgentChatSession, chat_id)
            assert chat.summary_upto_turn == 2 and "RF Gun 02" in chat.summary
            window = await load_window(db, chat, SETTINGS, 9)
            assert window[0].turn_no == 3
            assert await db.scalar(select(AgentLlmCall.id).limit(1)) is None  # no recorder
        again = await compact(
            factory, chat_id, settings=SETTINGS, target=TARGET, client_for=client_for
        )
        assert not again
    finally:
        await _cleanup(itsm_engine)
