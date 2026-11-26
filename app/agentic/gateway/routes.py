# app/agentic/gateway/routes.py
"""/api/v1/chat - the RIC Store Assistant over HTTP (store from X-Site-Id, user from
X-User, role from AGENT_DEV_USER_ROLES; Entra ID later changes only app/api/context.py).

    POST /api/v1/chat/messages      one turn -> AssistantResponse (busy / replayed are
                                    normal answers); compaction runs after the response
    GET  /api/v1/chat/session       the active chat: state, Confirm action, messages
    POST /api/v1/chat/session/new   close the active chat, start a new one
    POST /api/v1/chat/actions       confirm_incident / cancel_draft -> AssistantResponse
                                    (Confirm creates the GLPI ticket too, with the API's
                                    GLPI client app.state.itsm_client)

The agent stack is built on the first chat request and kept on app.state (app startup
is unchanged). The MCP server is a separate process at MCP_URL. Agent modules are
imported lazily, so importing the API stays light.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from sqlalchemy import select

from app.agentic.chat.schemas import AssistantResponse, TurnRequest
from app.agentic.gateway.confirm import find_active_chat, handle_action
from app.agentic.gateway.views import (
    ChatActionBody,
    ChatMessageBody,
    ChatMessageView,
    ChatSessionView,
    pending_action_for,
)
from app.api.context import MobileActorDep, StoreDep
from app.api.deps import ItsmClientDep
from app.core.errors import ServiceUnavailableError
from app.models import AgentChatMessage, AgentChatSession

if TYPE_CHECKING:
    from app.agentic.chat.turn import ChatDeps

router = APIRouter(prefix="/chat", tags=["chat"])
HISTORY_LIMIT = 40
UNAVAILABLE = "The assistant is switched off right now. Please try again later."


async def get_chat_deps(request: Request) -> Any:
    """The agent stack for this API process (built once, on the first chat request)."""
    state = request.app.state
    deps = getattr(state, "chat_deps", None)
    if deps is not None:
        return deps
    from app.agentic.chat.turn import build_chat_deps
    from app.agentic.config import get_agentic_settings
    from app.agentic.store_graph import load_configured_catalogue
    from mcp_server.runtime import load_checked_policy

    agentic = get_agentic_settings()
    try:
        deps = build_chat_deps(
            agentic,
            state.settings,
            state.engine,
            load_configured_catalogue(agentic),
            load_checked_policy(agentic),
            agentic.mcp_url,
            clock=state.clock,
        )
    except Exception as exc:
        raise ServiceUnavailableError(f"The assistant is not configured: {exc}") from exc
    state.chat_deps = deps
    return deps


ChatDepsDep = Annotated[Any, Depends(get_chat_deps)]


def _role(deps: ChatDeps, user: str) -> str:
    return str(deps.settings.role_of(user))


@router.post("/messages", response_model=AssistantResponse)
async def post_message(
    body: ChatMessageBody,
    store: StoreDep,
    actor: MobileActorDep,
    deps: ChatDepsDep,
    background: BackgroundTasks,
) -> AssistantResponse:
    if not deps.settings.agent_enabled:
        return AssistantResponse(
            session_id="",
            turn_no=None,
            correlation_id=str(uuid.uuid4()),
            answer=UNAVAILABLE,
            state="unavailable",
        )
    from app.agentic.chat.turn import compact_chat, run_turn

    response = await run_turn(
        deps,
        TurnRequest(
            store_id=store.id,
            user_id=actor.actor,
            role=_role(deps, actor.actor),
            text=body.text,
            client_message_id=body.client_message_id,
        ),
    )
    if not response.busy and not response.replayed and response.session_id:
        background.add_task(compact_chat, deps, uuid.UUID(response.session_id))
    return response


async def _session_view(deps: ChatDeps, store_id: uuid.UUID, user: str) -> ChatSessionView:
    from app.agentic.chat.state import derive_state

    role = _role(deps, user)
    chat_id = await find_active_chat(deps.factory, store_id, user)
    if chat_id is None:
        return ChatSessionView(session_id=None, state="idle", role=role)
    async with deps.factory() as session:
        chat = await session.get(AgentChatSession, chat_id)
        state = await derive_state(
            session,
            chat_id,
            dict(chat.state_data or {}) if chat else {},
            now=deps.clock.now(),
            abandon_minutes=deps.settings.kb_guide_abandon_minutes,
        )
        rows = (
            await session.execute(
                select(
                    AgentChatMessage.turn_no,
                    AgentChatMessage.role,
                    AgentChatMessage.content,
                    AgentChatMessage.response,
                    AgentChatMessage.created_at,
                )
                .where(AgentChatMessage.session_id == chat_id)
                .order_by(AgentChatMessage.turn_no.desc(), AgentChatMessage.role)
                .limit(HISTORY_LIMIT)
            )
        ).all()
    messages = [
        ChatMessageView(
            turn_no=r.turn_no,
            role=r.role,
            content=r.content,
            response=r.response,
            created_at=r.created_at,
        )
        for r in reversed(rows)
    ]
    return ChatSessionView(
        session_id=str(chat_id),
        state=str(state.state),
        role=role,
        pending_action=pending_action_for(state, role),
        messages=messages,
    )


@router.get("/session", response_model=ChatSessionView)
async def get_chat_session(
    store: StoreDep, actor: MobileActorDep, deps: ChatDepsDep
) -> ChatSessionView:
    return await _session_view(deps, store.id, actor.actor)


@router.post("/session/new", response_model=ChatSessionView)
async def new_chat_session(
    store: StoreDep, actor: MobileActorDep, deps: ChatDepsDep
) -> ChatSessionView:
    from app.agentic.chat.sessions import close_session, ensure_session

    now = deps.clock.now()
    current = await find_active_chat(deps.factory, store.id, actor.actor)
    if current is not None:
        await close_session(deps.factory, current, now)
    await ensure_session(
        deps.factory,
        store_id=store.id,
        user_id=actor.actor,
        role=_role(deps, actor.actor),
        now=now,
        idle_minutes=deps.settings.agent_session_idle_minutes,
    )
    return await _session_view(deps, store.id, actor.actor)


@router.post("/actions", response_model=AssistantResponse)
async def post_action(
    body: ChatActionBody,
    store: StoreDep,
    actor: MobileActorDep,
    deps: ChatDepsDep,
    glpi: ItsmClientDep,
) -> AssistantResponse:
    return await handle_action(
        deps,
        store_id=store.id,
        user_id=actor.actor,
        role=_role(deps, actor.actor),
        action=body.action,
        draft_id=body.draft_id,
        version=body.version,
        glpi=glpi,
    )
