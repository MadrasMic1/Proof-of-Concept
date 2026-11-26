# app/agentic/chat/turn.py
"""One chat turn (the gateway calls exactly this later):

    session (start / expire) -> turn lock -> client_message_id replay -> user message
    -> derived state -> snapshot (Phase 2) -> window -> orchestrator -> derived state
    -> compose -> assistant message + session update -> unlock
Compaction is separate (compact_chat), run by the caller AFTER the response is shown.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.agentic.agents.deps import build_agent_deps
from app.agentic.agents.runtime import AgentDeps
from app.agentic.agents.schemas import TrustedContext
from app.agentic.chat import compaction
from app.agentic.chat.composer import card_titles, compose
from app.agentic.chat.orchestrator import Orchestrator, TurnContext, diagnosis_of
from app.agentic.chat.schemas import AssistantResponse, TurnRequest
from app.agentic.chat.sessions import (
    TurnLock,
    add_message,
    ensure_session,
    find_client_message,
    load_window,
    render_window,
)
from app.agentic.chat.state import derive_state, state_block
from app.agentic.config import AgenticSettings
from app.agentic.drafts.triage import mask_text
from app.agentic.llm.client import (
    ModelTarget,
    ProfileName,
    build_chat_client,
    fallback_target,
    primary_target,
)
from app.agentic.llm.usage import database_recorder
from app.agentic.store_context.render import render_snapshot
from app.agentic.store_context.snapshot import build_snapshot
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.tools.policy import ToolPolicy
from app.agentic.values import ConversationState, MessageRole
from app.core.clock import Clock, SystemClock
from app.core.config import Settings
from app.db.session import create_session_factory
from app.models import AgentChatMessage, AgentChatSession

logger = logging.getLogger("ric.agents")
BUSY = "Still working on your previous message - please wait a moment."
FAILED = "Sorry, something went wrong while answering. Please try again."
MAX_TEXT = 2000


@dataclass
class ChatDeps:
    settings: AgenticSettings
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    catalogue: Catalogue
    agent_deps: AgentDeps
    orchestrator_targets: list[ModelTarget]
    summariser_target: ModelTarget
    client_for: Callable[[ModelTarget, ProfileName], Any]
    clock: Clock


def build_chat_deps(
    settings: AgenticSettings,
    core: Settings,
    engine: AsyncEngine,
    catalogue: Catalogue,
    policy: ToolPolicy,
    mcp_url: str,
    *,
    inner: Any | None = None,
    targets: list[ModelTarget] | None = None,
    clock: Clock | None = None,
) -> ChatDeps:
    """inner / targets: a scripted model for tests (every profile uses them)."""
    factory = create_session_factory(engine)
    agent_deps = build_agent_deps(
        settings, core, policy, mcp_url, usage_factory=factory, inner=inner, targets=targets
    )
    if targets is not None:
        orchestrator_targets, summariser = list(targets), targets[0]
    else:
        orchestrator_targets = [primary_target(settings, core, "orchestrator")]
        fallback = fallback_target(settings, core)
        if fallback is not None:
            orchestrator_targets.append(fallback)
        summariser = primary_target(settings, core, "summariser")
    recorder = database_recorder(factory)
    cache: dict[str, Any] = {}

    def client_for(target: ModelTarget, profile: ProfileName) -> Any:
        key = f"{profile}|{target.label}|{target.fallback}"
        if key not in cache:
            cache[key] = build_chat_client(
                target, settings, profile, recorder=recorder, inner=inner
            )
        return cache[key]

    return ChatDeps(
        settings=settings,
        engine=engine,
        factory=factory,
        catalogue=catalogue,
        agent_deps=agent_deps,
        orchestrator_targets=orchestrator_targets,
        summariser_target=summariser,
        client_for=client_for,
        clock=clock or SystemClock(),
    )


def _message(snapshot_text: str, state_text: str, window_text: str, user_text: str) -> str:
    return (
        "STORE SNAPSHOT (live, computed now)\n"
        f"{snapshot_text}\n\n"
        "CONVERSATION\n"
        f"{state_text}\n\n"
        f"{window_text}\n\n"
        f"USER MESSAGE\n{user_text}"
    )


async def run_turn(deps: ChatDeps, request: TurnRequest) -> AssistantResponse:
    settings = deps.settings
    now = deps.clock.now()
    correlation = uuid.uuid4()
    session_id = await ensure_session(
        deps.factory,
        store_id=request.store_id,
        user_id=request.user_id,
        role=request.role,
        now=now,
        idle_minutes=settings.agent_session_idle_minutes,
    )

    async def stored() -> AssistantResponse | None:
        if request.client_message_id is None:
            return None
        async with deps.factory() as session:
            found = await find_client_message(session, session_id, request.client_message_id)
        if found is None or found.response is None:
            return None
        return AssistantResponse.model_validate({**found.response, "replayed": True})

    lock = TurnLock(deps.engine, session_id)
    if not await lock.acquire():
        replay = await stored()
        if replay is not None:
            return replay
        return AssistantResponse(
            session_id=str(session_id),
            turn_no=None,
            correlation_id=str(correlation),
            answer=BUSY,
            state="busy",
            busy=True,
        )
    try:
        replay = await stored()
        if replay is not None:
            return replay
        return await _locked_turn(deps, request, session_id, correlation, now)
    finally:
        await lock.release()


async def _locked_turn(
    deps: ChatDeps,
    request: TurnRequest,
    session_id: uuid.UUID,
    correlation: uuid.UUID,
    now: Any,
) -> AssistantResponse:
    settings = deps.settings
    text = mask_text(request.text.strip()[:MAX_TEXT]) or ""
    async with deps.factory() as session:
        chat = await session.get(AgentChatSession, session_id)
        assert chat is not None
        last = await session.scalar(
            select(func.max(AgentChatMessage.turn_no)).where(
                AgentChatMessage.session_id == session_id
            )
        )
        turn_no = (last or 0) + 1  # never reuses a turn left half-done by a crash
        existing = None
        if request.client_message_id is not None:
            existing = await find_client_message(session, session_id, request.client_message_id)
            if existing is not None:  # an earlier attempt crashed before answering
                turn_no = existing.turn_no
        if existing is None:
            await add_message(
                session,
                session_id=session_id,
                store_id=request.store_id,
                turn_no=turn_no,
                role=MessageRole.USER,
                content=text,
                correlation_id=correlation,
                now=now,
                client_message_id=request.client_message_id,
            )
        state_data: dict[str, Any] = dict(chat.state_data or {})
        start = await derive_state(
            session,
            session_id,
            state_data,
            now=now,
            abandon_minutes=settings.kb_guide_abandon_minutes,
        )
        window = await load_window(session, chat, settings, turn_no)
        summary = chat.summary
        await session.commit()

    warnings: list[str] = []
    results: list[Any] = []
    answer: dict[str, Any] | None = None
    fallback_text = ""
    as_of = None
    stale = False
    try:
        snapshot = await build_snapshot(
            deps.factory,
            request.store_id,
            now=now,
            catalogue=deps.catalogue,
            settings=settings,
        )
        as_of, stale = snapshot.as_of, bool(snapshot.warnings)
        snapshot_text = render_snapshot(
            snapshot,
            max_tokens=settings.store_ctx_snapshot_max_tokens,
            max_device_lines=settings.store_ctx_max_device_lines,
            max_incident_lines=settings.store_ctx_max_incident_lines,
        )
    except Exception:
        logger.exception("snapshot failed for store %s", request.store_id)
        snapshot_text = "Store data is not available right now: do not state store status."
        warnings.append("store data unavailable")
    try:
        orchestrator = Orchestrator(
            settings=settings,
            agent_deps=deps.agent_deps,
            targets=deps.orchestrator_targets,
            client_for=lambda target: deps.client_for(target, "orchestrator"),
        )
        context = TurnContext(
            trusted=TrustedContext(
                request.user_id,
                request.role,
                request.store_id,
                correlation,
                session_id,
            ),
            start_state=start,
            state_data=state_data,
        )
        message = _message(
            snapshot_text,
            state_block(start, state_data.get("last_diagnosis"), request.role),
            render_window(summary, [w for w in window if w.turn_no < turn_no]),
            text,
        )
        outcome = await orchestrator.run(context, message)
        results, answer, fallback_text = outcome.results, outcome.answer, outcome.text
        warnings += outcome.warnings
    except Exception:
        logger.exception("turn failed for chat %s", session_id)
        warnings.append("the assistant failed on this turn")

    for result in results:
        if result.agent == "store_insight" and result.status == "ok":
            state_data["last_diagnosis"] = diagnosis_of(result)
        if (
            result.agent == "kb_guide"
            and result.status == "ok"
            and result.outcome in ("exhausted", "needs_technician")
            and result.body.get("guide_id")
        ):
            state_data["handoff_guide_id"] = result.body["guide_id"]
    acks_guides = list(state_data.get("ack_guides") or [])
    acks_drafts = list(state_data.get("ack_drafts") or [])
    if start.state in (ConversationState.GUIDE_RESOLVED, ConversationState.GUIDE_EXHAUSTED):
        acks_guides.append(str(start.guide.id) if start.guide else "")
    if start.state == ConversationState.INCIDENT_RAISED and start.confirmed_draft_id:
        acks_drafts.append(str(start.confirmed_draft_id))
    state_data["ack_guides"] = [g for g in dict.fromkeys(acks_guides) if g][-20:]
    state_data["ack_drafts"] = [d for d in dict.fromkeys(acks_drafts) if d][-20:]

    async with deps.factory() as session:
        end = await derive_state(
            session,
            session_id,
            state_data,
            now=deps.clock.now(),
            abandon_minutes=settings.kb_guide_abandon_minutes,
        )
        if end.state == ConversationState.GUIDE_RESOLVED and end.guide:
            state_data["ack_guides"] = [*state_data["ack_guides"], str(end.guide.id)][-20:]
        answer_text = (answer or {}).get("answer") or fallback_text or FAILED
        response = compose(
            session_id=session_id,
            turn_no=turn_no,
            correlation_id=correlation,
            answer=answer_text,
            requested_cards=(answer or {}).get("show_cards") or [],
            suggested_replies=(answer or {}).get("suggested_replies") or [],
            results=results,
            state=end,
            role=request.role,
            as_of=as_of,
            stale=stale,
            warnings=warnings,
            max_replies=settings.agent_max_suggested_replies,
        )
        payload = response.model_dump(mode="json")
        await add_message(
            session,
            session_id=session_id,
            store_id=request.store_id,
            turn_no=turn_no,
            role=MessageRole.ASSISTANT,
            content=f"{response.answer}{card_titles(payload)}"[:4000],
            correlation_id=correlation,
            now=deps.clock.now(),
            response=payload,
        )
        chat = await session.get(AgentChatSession, session_id)
        assert chat is not None
        chat.turn_count = max(chat.turn_count, turn_no)
        chat.last_activity_at = deps.clock.now()
        chat.state = end.state
        chat.state_data = {**state_data, "derived": end.as_json()}
        chat.state_version = chat.state_version + 1
        await session.commit()
    return response


async def compact_chat(deps: ChatDeps, session_id: uuid.UUID) -> bool:
    return await compaction.compact(
        deps.factory,
        session_id,
        settings=deps.settings,
        target=deps.summariser_target,
        client_for=lambda target: deps.client_for(target, "summariser"),
    )
