# app/agentic/gateway/confirm.py
"""Tap actions on an incident draft: confirm_incident and cancel_draft.

Confirm, under the chat's turn lock and in ONE transaction:
    1. the caller is a store manager; the draft is theirs (store, user, active chat),
       locked FOR UPDATE
    2. already confirmed -> the same incident number again (a double tap creates nothing)
    3. pending, not expired, and the version the card showed
    4. duplicate re-check on a fresh snapshot: a STRONG match (someone raised it in the
       meantime) -> draft superseded, no incident
    5. app.services.incidents.create_incident (unchanged) with the Phase 4 mapping, actor
       = the store manager, audit source 'agent'; the draft becomes confirmed with
       incident_id in the same transaction
    6. AGENT_CONFIRM_TO_GLPI=true: the GLPI ticket is created (app/itsm/tickets.py) BEFORE
       the commit and its id stored on the incident. GLPI down / refused / not
       configured / store not linked -> the whole transaction is rolled back: nothing
       is created, the draft stays pending (last_error set) and the manager can tap
       Confirm again. false: a RIC-only incident.
    7. one assistant message in the chat; the next turn derives incident_raised
Outcomes the user should read (expired, changed, superseded, GLPI unavailable) are normal
answers, not HTTP errors.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.chat.schemas import AssistantResponse
from app.agentic.chat.sessions import TurnLock, add_message
from app.agentic.chat.state import derive_state
from app.agentic.drafts.duplicates import check_duplicates
from app.agentic.drafts.mapping import draft_to_new_incident, store_manager_actor
from app.agentic.drafts.service import DraftContext, DraftError, cancel_draft, fresh_snapshot
from app.agentic.gateway.views import pending_action_for
from app.agentic.values import AgentRole, ChatSessionStatus, DraftStatus, MessageRole
from app.core.errors import AppError, NotFoundError
from app.itsm.errors import ItsmNotFound, ItsmRejected, ItsmUnavailable
from app.itsm.tickets import StoreNotLinkedError, TicketClient, push_new_incident
from app.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentIncidentDraft,
    AssignmentGroup,
    Incident,
)
from app.services.incidents import create_incident

if TYPE_CHECKING:
    from app.agentic.chat.turn import ChatDeps

Action = Literal["confirm_incident", "cancel_draft"]
BUSY = "Still working on your previous message - please tap again in a moment."


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class GlpiPushError(Exception):
    """The GLPI ticket could not be created: the confirm transaction is rolled back."""


GLPI_FAILED = (
    "The incident could not be sent to GLPI ({reason}). Nothing was raised - "
    "please tap Confirm again in a moment."
)


async def find_active_chat(
    factory: async_sessionmaker[AsyncSession], store_id: uuid.UUID, user_id: str
) -> uuid.UUID | None:
    async with factory() as session:
        return await session.scalar(
            select(AgentChatSession.id).where(
                AgentChatSession.store_id == store_id,
                AgentChatSession.user_id == user_id,
                AgentChatSession.status == ChatSessionStatus.ACTIVE,
            )
        )


async def _confirm(
    session: AsyncSession,
    deps: ChatDeps,
    draft: AgentIncidentDraft,
    version: int | None,
    now: datetime,
    glpi: TicketClient | None,
    warnings: list[str],
) -> str:
    if draft.status == DraftStatus.CONFIRMED:
        number = await session.scalar(
            select(Incident.number).where(Incident.id == draft.incident_id)
        )
        return f"Incident {number} is already raised for this."
    if draft.status == DraftStatus.PENDING and now >= draft.expires_at:
        draft.status = DraftStatus.EXPIRED
        draft.updated_at = now
        return "This draft has expired. Ask me again and I will prepare a new one."
    if draft.status != DraftStatus.PENDING:
        return f"This draft is {draft.status} and cannot be raised. Ask me again if it is needed."
    if version is not None and version != draft.version:
        return "The draft was changed. Please review it and tap Confirm again."
    if settings_require_glpi(deps) and glpi is None:
        raise GlpiPushError("GLPI is not configured on the API")
    settings = deps.settings
    snapshot = await fresh_snapshot(
        session, draft.store_id, now=now, catalogue=deps.catalogue, settings=settings
    )
    finding_ids = set(draft.finding_ids or [])
    finding = next((f for f in snapshot.findings if f.finding_id in finding_ids), None)
    check = await check_duplicates(
        session,
        snapshot,
        store_id=draft.store_id,
        chat_session_id=draft.chat_session_id,
        finding=finding,
        subject=None,
        text=draft.short_description,
        now=now,
        lookback_hours=settings.incident_dup_lookback_hours,
        min_score=settings.incident_dup_text_min_score,
    )
    strong = [m.incident_number for m in check.matches if m.level == "strong" and m.incident_number]
    if strong:
        draft.status = DraftStatus.SUPERSEDED
        draft.duplicate_check = {**(draft.duplicate_check or {}), "at_confirm": check.as_json()}
        draft.updated_at = now
        numbers = ", ".join(dict.fromkeys(strong))
        return f"This was just raised as {numbers}, so no new incident was created."
    incident = await create_incident(
        session,
        store_manager_actor(draft.user_id),
        draft.store_id,
        draft_to_new_incident(draft),
        now,
        commit=False,
    )
    ticket_text = ""
    if settings_require_glpi(deps):
        assert glpi is not None
        try:
            pushed = await push_new_incident(session, glpi, incident, raised_by=draft.user_id)
        except StoreNotLinkedError as exc:
            raise GlpiPushError(str(exc)) from exc
        except (ItsmUnavailable, ItsmRejected, ItsmNotFound) as exc:
            raise GlpiPushError(str(exc)[:160]) from exc
        warnings.extend(pushed.warnings)
        ticket_text = f" (GLPI ticket {pushed.ticket_id})"
    draft.status = DraftStatus.CONFIRMED
    draft.incident_id = incident.id
    draft.confirmed_at = now
    draft.last_error = None
    draft.updated_at = now
    group = None
    if draft.assignment_group_id is not None:
        group = await session.scalar(
            select(AssignmentGroup.name).where(AssignmentGroup.id == draft.assignment_group_id)
        )
    text = f"Incident {incident.number} raised{ticket_text}: {incident.short_description}."
    return f"{text} {group} will pick it up." if group else text


async def _cancel(
    session: AsyncSession, draft: AgentIncidentDraft, role: str, now: datetime
) -> str:
    if draft.status != DraftStatus.PENDING:
        return f"This draft is already {draft.status}."
    context = DraftContext(draft.store_id, draft.user_id, draft.chat_session_id, AgentRole(role))
    try:
        await cancel_draft(session, context, draft.id, now=now)
    except DraftError as exc:
        return exc.message
    return "The draft was cancelled. Nothing was raised."


def settings_require_glpi(deps: ChatDeps) -> bool:
    return bool(deps.settings.agent_confirm_to_glpi)


async def handle_action(
    deps: ChatDeps,
    *,
    store_id: uuid.UUID,
    user_id: str,
    role: str,
    action: Action,
    draft_id: uuid.UUID,
    version: int | None,
    glpi: TicketClient | None = None,
) -> AssistantResponse:
    if role != AgentRole.STORE_MANAGER:
        raise ForbiddenError("Only a store manager can confirm or cancel an incident draft.")
    chat_id = await find_active_chat(deps.factory, store_id, user_id)
    if chat_id is None:
        raise NotFoundError("There is no active chat for this user in this store.")
    lock = TurnLock(deps.engine, chat_id)
    if not await lock.acquire():
        return AssistantResponse(
            session_id=str(chat_id),
            turn_no=None,
            correlation_id=str(uuid.uuid4()),
            answer=BUSY,
            state="busy",
            busy=True,
        )
    try:
        async with deps.factory() as session:
            now = deps.clock.now()
            query = (
                select(AgentIncidentDraft)
                .where(
                    AgentIncidentDraft.id == draft_id,
                    AgentIncidentDraft.store_id == store_id,
                    AgentIncidentDraft.user_id == user_id,
                    AgentIncidentDraft.chat_session_id == chat_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            draft = await session.scalar(query)
            if draft is None:
                raise NotFoundError(f"Draft {draft_id} was not found.")
            warnings: list[str] = []
            if action == "confirm_incident":
                try:
                    text = await _confirm(session, deps, draft, version, now, glpi, warnings)
                except GlpiPushError as exc:
                    await session.rollback()
                    draft = await session.scalar(query)
                    assert draft is not None
                    draft.last_error = f"GLPI: {exc}"[:500]
                    draft.updated_at = now
                    text = GLPI_FAILED.format(reason=str(exc)[:160])
            else:
                text = await _cancel(session, draft, role, now)
            return await _finish(session, deps, chat_id, store_id, role, text, now, warnings)
    finally:
        await lock.release()


async def _finish(
    session: AsyncSession,
    deps: ChatDeps,
    chat_id: uuid.UUID,
    store_id: uuid.UUID,
    role: str,
    text: str,
    now: datetime,
    warnings: list[str] | None = None,
) -> AssistantResponse:
    await session.flush()
    chat = await session.get(AgentChatSession, chat_id)
    assert chat is not None
    state_data = dict(chat.state_data or {})
    abandon = deps.settings.kb_guide_abandon_minutes
    state = await derive_state(session, chat_id, state_data, now=now, abandon_minutes=abandon)
    last = await session.scalar(
        select(func.max(AgentChatMessage.turn_no)).where(AgentChatMessage.session_id == chat_id)
    )
    turn_no = (last or 0) + 1
    correlation = uuid.uuid4()
    response = AssistantResponse(
        session_id=str(chat_id),
        turn_no=turn_no,
        correlation_id=str(correlation),
        answer=text,
        pending_action=pending_action_for(state, role),
        state=str(state.state),
        warnings=list(warnings or []),
    )
    await add_message(
        session,
        session_id=chat_id,
        store_id=store_id,
        turn_no=turn_no,
        role=MessageRole.ASSISTANT,
        content=text,
        correlation_id=correlation,
        now=now,
        response=response.model_dump(mode="json"),
    )
    chat.turn_count = max(chat.turn_count, turn_no)
    chat.last_activity_at = now
    chat.state = state.state
    chat.state_data = {**state_data, "derived": state.as_json()}
    chat.state_version = chat.state_version + 1
    await session.commit()
    return response
