# app/models/agentic.py
"""Group 7: the agentic system (migration 0005). Allowed values: app/agentic/values.py.

As for the other groups, the database owns CHECK constraints, composite (id, store_id)
foreign keys, partial indexes and triggers; the models describe columns and defaults.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Computed,
    ForeignKey,
    Identity,
    Integer,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.agentic.values import EMBEDDING_DIM
from app.db.base import Base, CreatedAt, JsonObject, UpdatedAt, UUIDPk

EMPTY_ARRAY = text("'{}'")
EMPTY_JSON_ARRAY = text("'[]'::jsonb")


class AgentStoreDependency(Base):
    __tablename__ = "agent_store_dependencies"

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    from_kind: Mapped[str]
    from_device_id: Mapped[uuid.UUID | None]
    from_element_id: Mapped[uuid.UUID | None]
    to_kind: Mapped[str]
    to_device_id: Mapped[uuid.UUID | None]
    to_element_id: Mapped[uuid.UUID | None]
    to_service: Mapped[str | None]
    relation: Mapped[str]
    redundancy_group: Mapped[str | None]
    note: Mapped[str | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AgentChatSession(Base):
    __tablename__ = "agent_chat_sessions"
    __table_args__ = (UniqueConstraint("id", "store_id"),)

    id: Mapped[UUIDPk]
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    user_id: Mapped[str]
    role: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'active'"))
    state: Mapped[str] = mapped_column(server_default=text("'idle'"))
    state_data: Mapped[JsonObject]
    state_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    summary: Mapped[str | None]
    summary_upto_turn: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    turn_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_activity_at: Mapped[datetime] = mapped_column(server_default=func.now())
    closed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AgentChatMessage(Base):
    __tablename__ = "agent_chat_messages"
    __table_args__ = (UniqueConstraint("session_id", "turn_no", "role"),)

    id: Mapped[UUIDPk]
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_chat_sessions.id"))
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    turn_no: Mapped[int] = mapped_column(Integer)
    role: Mapped[str]
    content: Mapped[str]
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    client_message_id: Mapped[uuid.UUID | None]
    correlation_id: Mapped[uuid.UUID]
    created_at: Mapped[CreatedAt]


class AgentKbArticle(Base):
    __tablename__ = "agent_kb_articles"
    __table_args__ = (UniqueConstraint("kb_ref", "version"),)

    id: Mapped[UUIDPk]
    kb_ref: Mapped[str]
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(server_default=text("'published'"))
    title: Mapped[str]
    summary: Mapped[str]
    applies_to: Mapped[list[str]] = mapped_column(ARRAY(Text))
    fault_codes: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    kb_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    audience: Mapped[str] = mapped_column(server_default=text("'store_staff'"))
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str]
    published_at: Mapped[datetime] = mapped_column(server_default=func.now())
    retired_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AgentKbChunk(Base):
    __tablename__ = "agent_kb_chunks"

    id: Mapped[UUIDPk]
    article_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_kb_articles.id", ondelete="CASCADE")
    )
    chunk_type: Mapped[str]
    step_id: Mapped[str | None]
    chunk_text: Mapped[str]
    text_hash: Mapped[str]
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    embedding_model: Mapped[str | None]
    tsv: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', chunk_text)", persisted=True)
    )
    created_at: Mapped[CreatedAt]


class AgentKbGuideSession(Base):
    __tablename__ = "agent_kb_guide_sessions"
    __table_args__ = (UniqueConstraint("id", "store_id"),)

    id: Mapped[UUIDPk]
    chat_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_chat_sessions.id"))
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    user_id: Mapped[str]
    article_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_kb_articles.id"))
    kb_ref: Mapped[str]
    kb_version: Mapped[int] = mapped_column(Integer)
    subject_device_id: Mapped[uuid.UUID | None]
    subject_element_id: Mapped[uuid.UUID | None]
    finding_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    current_step: Mapped[str]
    status: Mapped[str] = mapped_column(server_default=text("'in_progress'"))
    clarifications: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))
    history: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_JSON_ARRAY)
    state_version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    ended_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AgentIncidentDraft(Base):
    __tablename__ = "agent_incident_drafts"

    id: Mapped[UUIDPk]
    chat_session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agent_chat_sessions.id"))
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id"))
    user_id: Mapped[str]
    guide_session_id: Mapped[uuid.UUID | None]
    status: Mapped[str] = mapped_column(server_default=text("'pending'"))
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))
    short_description: Mapped[str]
    description: Mapped[str]
    u_symptom: Mapped[str | None]
    category: Mapped[str]
    subcategory: Mapped[str | None]
    impact: Mapped[int] = mapped_column(SmallInteger)
    urgency: Mapped[int] = mapped_column(SmallInteger)
    priority: Mapped[int | None] = mapped_column(
        SmallInteger, Computed("impact + urgency - 1", persisted=True)
    )
    baseline_impact: Mapped[int] = mapped_column(SmallInteger)
    baseline_urgency: Mapped[int] = mapped_column(SmallInteger)
    reasons: Mapped[JsonObject]
    assignment_group_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("assignment_groups.id")
    )
    cmdb_ci_network_element_id: Mapped[uuid.UUID | None]
    cmdb_ci_device_id: Mapped[uuid.UUID | None]
    impacted_nodes: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_JSON_ARRAY)
    finding_ids: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default=EMPTY_ARRAY)
    evidence: Mapped[JsonObject]
    steps_tried: Mapped[list[Any]] = mapped_column(JSONB, server_default=EMPTY_JSON_ARRAY)
    duplicate_check: Mapped[JsonObject]
    incident_id: Mapped[uuid.UUID | None] = mapped_column(unique=True)
    last_error: Mapped[str | None]
    expires_at: Mapped[datetime]
    confirmed_at: Mapped[datetime | None]
    created_at: Mapped[CreatedAt]
    updated_at: Mapped[UpdatedAt]


class AgentLlmCall(Base):
    __tablename__ = "agent_llm_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    correlation_id: Mapped[uuid.UUID]
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_chat_sessions.id"))
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stores.id"))
    user_id: Mapped[str | None]
    agent: Mapped[str]
    profile: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    fallback_used: Mapped[bool] = mapped_column(server_default=text("false"))
    status: Mapped[str]
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None]


class AgentToolCall(Base):
    __tablename__ = "agent_tool_calls"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    correlation_id: Mapped[uuid.UUID]
    chat_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_chat_sessions.id"))
    store_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("stores.id"))
    user_id: Mapped[str | None]
    role: Mapped[str | None]
    agent: Mapped[str | None]
    tool: Mapped[str]
    outcome: Mapped[str]
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    args_hash: Mapped[str | None]
    policy_version: Mapped[str | None]
    catalogue_version: Mapped[str | None]
    error_message: Mapped[str | None]
