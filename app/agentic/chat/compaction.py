# app/agentic/chat/compaction.py
"""Fold old turns into the running summary (summariser profile, one model call, no
tools). Runs AFTER the response; a failure leaves the turns as they are (the window
then shows up to AGENT_MEMORY_HARD_CAP_TURNS)."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import agent_framework as af
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.config import AgenticSettings
from app.agentic.llm.client import ModelTarget
from app.agentic.llm.transport import CURRENT_RUN, RunState
from app.models import AgentChatMessage, AgentChatSession

logger = logging.getLogger("ric.agents")
PROMPT = Path(__file__).resolve().parents[1] / "agents" / "prompts" / "summariser.v1.md"
CHARS_PER_TOKEN = 4


async def compact(
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    *,
    settings: AgenticSettings,
    target: ModelTarget,
    client_for: Callable[[ModelTarget], Any],
) -> bool:
    try:
        async with factory() as session:
            chat = await session.get(AgentChatSession, session_id)
            if chat is None:
                return False
            upto = chat.turn_count - settings.agent_memory_verbatim_turns
            if upto <= chat.summary_upto_turn:
                return False
            rows = (
                await session.execute(
                    select(
                        AgentChatMessage.turn_no,
                        AgentChatMessage.role,
                        AgentChatMessage.content,
                    )
                    .where(
                        AgentChatMessage.session_id == session_id,
                        AgentChatMessage.turn_no > chat.summary_upto_turn,
                        AgentChatMessage.turn_no <= upto,
                    )
                    .order_by(AgentChatMessage.turn_no, AgentChatMessage.role.desc())
                )
            ).all()
            previous, store_id, user_id = chat.summary, chat.store_id, chat.user_id
        lines = [f"{'User' if r.role == 'user' else 'Assistant'}: {r.content}" for r in rows]
        message = f"Summary so far:\n{previous or '(none)'}\n\nNew turns to add:\n" + "\n".join(
            lines
        )
        state = RunState(
            agent="summariser",
            profile="summariser",
            correlation_id=uuid.uuid4(),
            max_model_calls=1,
            store_id=store_id,
            chat_session_id=session_id,
            user_id=user_id,
        )
        reset = CURRENT_RUN.set(state)
        try:
            agent = af.Agent(
                client_for(target), PROMPT.read_text(encoding="utf-8"), name="summariser"
            )
            response = await asyncio.wait_for(
                agent.run(message), settings.agent_llm_timeout_seconds
            )
        finally:
            CURRENT_RUN.reset(reset)
        summary = (getattr(response, "text", None) or "").strip()
        if not summary:
            return False
        summary = summary[: settings.agent_memory_summary_max_tokens * CHARS_PER_TOKEN]
        async with factory() as session:
            chat = await session.get(AgentChatSession, session_id)
            if chat is None or chat.summary_upto_turn >= upto:
                return False
            chat.summary = summary
            chat.summary_upto_turn = upto
            await session.commit()
        return True
    except Exception:
        logger.exception("compaction failed for chat %s", session_id)
        return False
