# app/agentic/gateway/views.py
"""Request bodies and read views of the chat API (the turn itself returns the Phase 7
AssistantResponse). Bodies accept camelCase (app) and snake_case (scripts)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.agentic.chat.schemas import PendingAction
from app.agentic.chat.state import DerivedState
from app.agentic.values import AgentRole, ConversationState


class _Body(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
    )


class ChatMessageBody(_Body):
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: uuid.UUID | None = None


class ChatActionBody(_Body):
    action: Literal["confirm_incident", "cancel_draft"]
    draft_id: uuid.UUID
    version: int | None = Field(default=None, ge=1)


class ChatMessageView(BaseModel):
    turn_no: int
    role: str
    content: str
    response: dict[str, Any] | None = None
    created_at: datetime


class ChatSessionView(BaseModel):
    session_id: str | None
    state: str
    role: str
    pending_action: PendingAction | None = None
    messages: list[ChatMessageView] = Field(default_factory=list)


def pending_action_for(state: DerivedState, role: str) -> PendingAction | None:
    """The Confirm button: only from the derived state, only for a store manager."""
    if (
        state.state != ConversationState.DRAFT_PENDING
        or state.draft is None
        or role != AgentRole.STORE_MANAGER
    ):
        return None
    return PendingAction(
        draft_id=str(state.draft.id),
        version=state.draft.version,
        expires_at=state.draft.expires_at,
        short_description=state.draft.short_description,
        priority=state.draft.priority,
    )
