# app/agentic/chat/state.py
"""The conversation state, DERIVED from the guide and draft rows (never written by a
model). Precedence: draft_pending > guiding > incident_raised > guide ended > idle.

An ending (a guide that ended, a confirmed draft) shows in the state for ONE turn after
it happened: that turn's end acknowledges it (state_data.ack_guides / ack_drafts), so
later turns return to idle. A resolved guide is acknowledged at the end of the turn it
ended in (the user was just told).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.values import ConversationState, DraftStatus, GuideStatus
from app.models import AgentIncidentDraft, AgentKbArticle, AgentKbGuideSession, Incident

ENDED = (GuideStatus.RESOLVED, GuideStatus.EXHAUSTED, GuideStatus.NEEDS_TECHNICIAN)


@dataclass(frozen=True)
class GuideInfo:
    id: uuid.UUID
    kb_ref: str
    title: str
    status: str
    current_step: str
    ask: str | None


@dataclass(frozen=True)
class DraftInfo:
    id: uuid.UUID
    version: int
    expires_at: datetime
    short_description: str
    priority: int


@dataclass(frozen=True)
class DerivedState:
    state: ConversationState
    guide: GuideInfo | None = None  # guiding, or the ended guide
    draft: DraftInfo | None = None  # draft_pending
    incident_number: str | None = None  # incident_raised
    confirmed_draft_id: uuid.UUID | None = None

    def as_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"state": str(self.state)}
        if self.guide:
            data["guide"] = {
                "id": str(self.guide.id),
                "kb_ref": self.guide.kb_ref,
                "status": self.guide.status,
                "current_step": self.guide.current_step,
            }
        if self.draft:
            data["draft"] = {"id": str(self.draft.id), "version": self.draft.version}
        if self.incident_number:
            data["incident_number"] = self.incident_number
        return data


def _guide(row: Any) -> GuideInfo:
    content = row.content or {}
    step = (content.get("steps") or {}).get(row.current_step) or {}
    return GuideInfo(
        id=row.id,
        kb_ref=row.kb_ref,
        title=row.title,
        status=row.status,
        current_step=row.current_step,
        ask=step.get("ask"),
    )


async def derive_state(
    session: AsyncSession,
    chat_id: uuid.UUID,
    state_data: dict[str, Any],
    *,
    now: datetime,
    abandon_minutes: int,
) -> DerivedState:
    draft = (
        await session.execute(
            select(
                AgentIncidentDraft.id,
                AgentIncidentDraft.version,
                AgentIncidentDraft.expires_at,
                AgentIncidentDraft.short_description,
                AgentIncidentDraft.impact,
                AgentIncidentDraft.urgency,
            )
            .where(
                AgentIncidentDraft.chat_session_id == chat_id,
                AgentIncidentDraft.status == DraftStatus.PENDING,
                AgentIncidentDraft.expires_at > now,
            )
            .order_by(AgentIncidentDraft.created_at.desc())
            .limit(1)
        )
    ).first()
    if draft is not None:
        return DerivedState(
            ConversationState.DRAFT_PENDING,
            draft=DraftInfo(
                draft.id,
                draft.version,
                draft.expires_at,
                draft.short_description,
                draft.impact + draft.urgency - 1,
            ),
        )
    guide_columns = (
        AgentKbGuideSession.id,
        AgentKbGuideSession.kb_ref,
        AgentKbGuideSession.status,
        AgentKbGuideSession.current_step,
        AgentKbArticle.title,
        AgentKbArticle.content,
    )
    active = (
        await session.execute(
            select(*guide_columns)
            .join(AgentKbArticle, AgentKbArticle.id == AgentKbGuideSession.article_id)
            .where(
                AgentKbGuideSession.chat_session_id == chat_id,
                AgentKbGuideSession.status == GuideStatus.IN_PROGRESS,
                AgentKbGuideSession.updated_at > now - timedelta(minutes=abandon_minutes),
            )
            .limit(1)
        )
    ).first()
    if active is not None:
        return DerivedState(ConversationState.GUIDING, guide=_guide(active))
    acked_drafts = set(state_data.get("ack_drafts") or [])
    raised = (
        await session.execute(
            select(AgentIncidentDraft.id, Incident.number)
            .join(Incident, Incident.id == AgentIncidentDraft.incident_id)
            .where(
                AgentIncidentDraft.chat_session_id == chat_id,
                AgentIncidentDraft.status == DraftStatus.CONFIRMED,
            )
            .order_by(AgentIncidentDraft.confirmed_at.desc())
            .limit(1)
        )
    ).first()
    if raised is not None and str(raised.id) not in acked_drafts:
        return DerivedState(
            ConversationState.INCIDENT_RAISED,
            incident_number=raised.number,
            confirmed_draft_id=raised.id,
        )
    acked_guides = set(state_data.get("ack_guides") or [])
    ended = (
        await session.execute(
            select(*guide_columns)
            .join(AgentKbArticle, AgentKbArticle.id == AgentKbGuideSession.article_id)
            .where(
                AgentKbGuideSession.chat_session_id == chat_id,
                AgentKbGuideSession.status.in_(ENDED),
            )
            .order_by(AgentKbGuideSession.ended_at.desc())
            .limit(1)
        )
    ).first()
    if ended is not None and str(ended.id) not in acked_guides:
        state = (
            ConversationState.GUIDE_RESOLVED
            if ended.status == GuideStatus.RESOLVED
            else ConversationState.GUIDE_EXHAUSTED
        )
        return DerivedState(state, guide=_guide(ended))
    return DerivedState(ConversationState.IDLE)


def state_block(state: DerivedState, last_diagnosis: dict[str, Any] | None, role: str) -> str:
    """The plain-language state the orchestrator reads (no ids)."""
    lines: list[str] = []
    s = state.state
    if s == ConversationState.GUIDING and state.guide:
        g = state.guide
        lines.append(
            f"Conversation state: GUIDING - the guide '{g.title}' ({g.kb_ref}) waits for the "
            f'user\'s answer to step {g.current_step}: "{g.ask}". A reply about this step -> '
            "ask_kb_guide(mode='continue', user_report=<the user's words>)."
        )
    elif s == ConversationState.GUIDE_RESOLVED and state.guide:
        lines.append(
            f"Conversation state: GUIDE_RESOLVED - the user fixed it with '{state.guide.title}'. "
            "Acknowledge briefly; nothing else to do unless asked."
        )
    elif s == ConversationState.GUIDE_EXHAUSTED and state.guide:
        lines.append(
            f"Conversation state: GUIDE_EXHAUSTED - the guide '{state.guide.title}' ended "
            f"({state.guide.status}) without fixing it; the steps tried are recorded."
        )
    elif s == ConversationState.DRAFT_PENDING and state.draft:
        d = state.draft
        lines.append(
            f'Conversation state: DRAFT_PENDING - a draft incident "{d.short_description}" '
            f"(priority {d.priority}) waits for the store manager to tap Confirm in the app, "
            f"until {d.expires_at:%H:%M} UTC. If the user agrees to raise it, tell them to tap "
            "Confirm on the draft. Requested changes -> ask_incident_desk(mode='revise')."
        )
    elif s == ConversationState.INCIDENT_RAISED:
        lines.append(
            f"Conversation state: INCIDENT_RAISED - incident {state.incident_number} was raised "
            "from this chat. Give its number if relevant."
        )
    else:
        lines.append("Conversation state: IDLE.")
    if last_diagnosis and last_diagnosis.get("summary"):
        lines.append(f"Last diagnosis in this chat: {last_diagnosis['summary']}")
    if role == "store_manager":
        lines.append("User role: store_manager (can raise incidents: drafts, then Confirm).")
    else:
        lines.append(
            "User role: store_staff (cannot raise incidents: suggest asking the store manager)."
        )
    return "\n".join(lines)
