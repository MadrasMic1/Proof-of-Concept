# app/agentic/chat/schemas.py
"""The turn request and the AssistantResponse every turn returns (the app's contract)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

CardKind = Literal[
    "finding",
    "device_options",
    "kb_step",
    "kb_choices",
    "incident_list",
    "incident_draft",
    "duplicate_notice",
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TurnRequest(_Model):
    store_id: uuid.UUID
    user_id: str = Field(min_length=1, max_length=120)
    role: Literal["store_staff", "store_manager"]
    text: str = Field(min_length=1, max_length=2000)
    client_message_id: uuid.UUID | None = None


class Card(_Model):
    kind: CardKind
    title: str
    data: dict[str, Any]


class PendingAction(_Model):
    type: Literal["confirm_incident"] = "confirm_incident"
    draft_id: str
    version: int
    expires_at: datetime
    short_description: str
    priority: int


class Freshness(_Model):
    as_of: datetime | None = None
    stale_warning: bool = False


class AssistantResponse(_Model):
    session_id: str
    turn_no: int | None
    correlation_id: str
    answer: str
    cards: list[Card] = Field(default_factory=list)
    suggested_replies: list[str] = Field(default_factory=list)
    pending_action: PendingAction | None = None
    state: str
    freshness: Freshness = Field(default_factory=Freshness)
    warnings: list[str] = Field(default_factory=list)
    replayed: bool = False
    busy: bool = False
