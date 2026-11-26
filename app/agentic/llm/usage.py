# app/agentic/llm/usage.py
"""Write one agent_llm_calls row per model request (app login, own short transaction)."""

from __future__ import annotations

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.llm.transport import LlmCallRecord, Recorder, RunState
from app.models import AgentLlmCall


def database_recorder(factory: async_sessionmaker[AsyncSession]) -> Recorder:
    async def record(state: RunState, call: LlmCallRecord) -> None:
        row = {
            "correlation_id": state.correlation_id,
            "chat_session_id": state.chat_session_id,
            "store_id": state.store_id,
            "user_id": state.user_id,
            "agent": state.agent,
            "profile": state.profile,
            "provider": call.provider[:40],
            "model": call.model[:120],
            "fallback_used": state.fallback_used,
            "status": call.status,
            "prompt_tokens": call.prompt_tokens,
            "completion_tokens": call.completion_tokens,
            "latency_ms": call.latency_ms,
            "error_code": call.error_code,
        }
        for attempt in (row, {**row, "chat_session_id": None, "store_id": None}):
            try:
                async with factory() as session:
                    await session.execute(insert(AgentLlmCall).values(**attempt))
                    await session.commit()
                return
            except IntegrityError:
                continue

    return record
