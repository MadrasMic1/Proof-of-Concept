# app/agentic/tools/base.py
"""What every tool function receives and returns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.common.tokens import ToolIdentity
from app.agentic.config import AgenticSettings
from app.agentic.kb.embeddings import Embedder
from app.agentic.store_context.assemble import (
    StoreSnapshot,
    assemble_snapshot,
    build_store_graph,
)
from app.agentic.store_context.snapshot import read_store_state
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.store_graph.model import GraphError, StoreGraph
from app.agentic.values import ErrorCode
from app.models import AgentChatSession


class Args(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ToolFailure(Exception):
    """A tool's expected failure; becomes an error envelope, never an MCP error."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ToolContext:
    identity: ToolIdentity
    now: datetime
    settings: AgenticSettings
    catalogue: Catalogue
    embedder: Embedder | None = None

    @property
    def store_id(self) -> uuid.UUID:
        return self.identity.store_id

    def chat_session_id(self) -> uuid.UUID:
        if self.identity.chat_session_id is None:
            raise ToolFailure(ErrorCode.INVALID_INPUT, "this tool needs a chat session")
        return self.identity.chat_session_id


@dataclass
class ToolOutput:
    data: dict[str, Any]
    as_of: datetime
    untrusted: list[str] = field(default_factory=list)  # paths in data, e.g. "items[].text"


@dataclass(frozen=True)
class LoadedStore:
    snapshot: StoreSnapshot
    graph: StoreGraph | None


def stale_after(settings: AgenticSettings) -> timedelta | None:
    if not settings.store_ctx_stale_check:
        return None
    return timedelta(minutes=settings.store_ctx_stale_after_minutes)


async def load_store(session: AsyncSession, ctx: ToolContext) -> LoadedStore:
    """The live snapshot and graph of the token's store (same read as the snapshot)."""
    state = await read_store_state(session, ctx.store_id, now=ctx.now, settings=ctx.settings)
    snapshot = assemble_snapshot(state, ctx.catalogue, now=ctx.now, settings=ctx.settings)
    try:
        graph, _ = build_store_graph(
            state, ctx.catalogue, now=ctx.now, stale_after=stale_after(ctx.settings)
        )
    except GraphError:
        graph = None
    return LoadedStore(snapshot, graph)


async def require_chat(session: AsyncSession, ctx: ToolContext) -> uuid.UUID:
    chat_id = ctx.chat_session_id()
    found = await session.scalar(
        select(AgentChatSession.id).where(
            AgentChatSession.id == chat_id,
            AgentChatSession.store_id == ctx.store_id,
            AgentChatSession.user_id == ctx.identity.user_id,
        )
    )
    if found is None:
        raise ToolFailure(ErrorCode.NOT_FOUND, "chat session not found")
    return chat_id


def parse_uuid(value: str, label: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise ToolFailure(ErrorCode.INVALID_INPUT, f"{label} must be a uuid") from exc
