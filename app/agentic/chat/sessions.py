# app/agentic/chat/sessions.py
"""Chat sessions and messages (app login).

- One active chat per user per store (partial unique index of migration 0005); a
  session idle longer than AGENT_SESSION_IDLE_MINUTES is expired on the next message.
- TurnLock: a SESSION-level advisory lock on one dedicated AUTOCOMMIT connection for
  the whole turn (no transaction stays open; a crash closes the connection and
  PostgreSQL releases the lock).
- client_message_id: a retried message returns the stored response, no model call.
- The window: the last AGENT_MEMORY_VERBATIM_TURNS turns not yet in the summary (up
  to AGENT_MEMORY_HARD_CAP_TURNS when compaction is behind).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import null, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.agentic.config import AgenticSettings
from app.agentic.values import ChatSessionStatus, ConversationState, MessageRole
from app.models import AgentChatMessage, AgentChatSession


def lock_key(session_id: uuid.UUID) -> int:
    return int.from_bytes(session_id.bytes[:8], "big", signed=True)


class TurnLock:
    def __init__(self, engine: AsyncEngine, session_id: uuid.UUID) -> None:
        self.engine = engine.execution_options(isolation_level="AUTOCOMMIT")
        self.key = lock_key(session_id)
        self._conn: AsyncConnection | None = None

    async def acquire(self) -> bool:
        conn = await self.engine.connect()
        got = await conn.scalar(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key})
        if not got:
            await conn.close()
            return False
        self._conn = conn
        return True

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": self.key})
        finally:
            await self._conn.close()
            self._conn = None


async def ensure_session(
    factory: async_sessionmaker[AsyncSession],
    *,
    store_id: uuid.UUID,
    user_id: str,
    role: str,
    now: datetime,
    idle_minutes: int,
) -> uuid.UUID:
    """The user's active chat in this store; a new one when none or it went idle."""
    query = select(AgentChatSession).where(
        AgentChatSession.store_id == store_id,
        AgentChatSession.user_id == user_id,
        AgentChatSession.status == ChatSessionStatus.ACTIVE,
    )
    async with factory() as session:
        row = await session.scalar(query)
        if row is not None and now - row.last_activity_at > timedelta(minutes=idle_minutes):
            row.status = ChatSessionStatus.EXPIRED
            row.closed_at = now
            await session.flush()
            row = None
        if row is not None:
            if row.role != role:
                row.role = role
            await session.commit()
            return row.id
        new_id = uuid.uuid4()
        session.add(
            AgentChatSession(
                id=new_id,
                store_id=store_id,
                user_id=user_id,
                role=role,
                status=ChatSessionStatus.ACTIVE,
                state=ConversationState.IDLE,
                state_data={},
                last_activity_at=now,
            )
        )
        try:
            await session.commit()
            return new_id
        except IntegrityError:
            await session.rollback()
    async with factory() as session:
        existing = await session.scalar(query)
        if existing is None:
            raise RuntimeError("could not start a chat session")
        return existing.id


async def close_session(
    factory: async_sessionmaker[AsyncSession], session_id: uuid.UUID, now: datetime
) -> None:
    async with factory() as session:
        await session.execute(
            update(AgentChatSession)
            .where(
                AgentChatSession.id == session_id,
                AgentChatSession.status == ChatSessionStatus.ACTIVE,
            )
            .values(status=ChatSessionStatus.CLOSED, closed_at=now)
        )
        await session.commit()


@dataclass(frozen=True)
class StoredTurn:
    turn_no: int
    response: dict[str, Any] | None


async def find_client_message(
    session: AsyncSession, session_id: uuid.UUID, client_message_id: uuid.UUID
) -> StoredTurn | None:
    turn_no = await session.scalar(
        select(AgentChatMessage.turn_no).where(
            AgentChatMessage.session_id == session_id,
            AgentChatMessage.client_message_id == client_message_id,
        )
    )
    if turn_no is None:
        return None
    response = await session.scalar(
        select(AgentChatMessage.response).where(
            AgentChatMessage.session_id == session_id,
            AgentChatMessage.turn_no == turn_no,
            AgentChatMessage.role == MessageRole.ASSISTANT,
        )
    )
    return StoredTurn(turn_no, response)


async def add_message(
    session: AsyncSession,
    *,
    session_id: uuid.UUID,
    store_id: uuid.UUID,
    turn_no: int,
    role: MessageRole,
    content: str,
    correlation_id: uuid.UUID,
    now: datetime,
    response: dict[str, Any] | None = None,
    client_message_id: uuid.UUID | None = None,
) -> None:
    session.add(
        AgentChatMessage(
            id=uuid.uuid4(),
            session_id=session_id,
            store_id=store_id,
            turn_no=turn_no,
            role=role,
            content=content,
            response=response if response is not None else null(),
            client_message_id=client_message_id,
            correlation_id=correlation_id,
            created_at=now,
        )
    )
    await session.flush()


@dataclass(frozen=True)
class WindowTurn:
    turn_no: int
    role: str
    content: str


async def load_window(
    session: AsyncSession, chat: AgentChatSession, settings: AgenticSettings, before_turn: int
) -> list[WindowTurn]:
    first = max(
        chat.summary_upto_turn + 1,
        before_turn - settings.agent_memory_hard_cap_turns,
    )
    rows = (
        await session.execute(
            select(AgentChatMessage.turn_no, AgentChatMessage.role, AgentChatMessage.content)
            .where(
                AgentChatMessage.session_id == chat.id,
                AgentChatMessage.turn_no >= first,
                AgentChatMessage.turn_no < before_turn,
            )
            .order_by(AgentChatMessage.turn_no, AgentChatMessage.role.desc())
        )
    ).all()
    return [WindowTurn(r.turn_no, r.role, r.content) for r in rows]


def render_window(summary: str | None, turns: list[WindowTurn]) -> str:
    lines: list[str] = []
    if summary:
        lines += ["Summary of the earlier conversation:", summary.strip(), ""]
    if turns:
        lines.append("Recent conversation:")
        lines += [f"{'User' if t.role == 'user' else 'Assistant'}: {t.content}" for t in turns]
    return "\n".join(lines) if lines else "This is the start of the conversation."
