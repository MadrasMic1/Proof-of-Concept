# tests/agentic/test_chat_live.py
"""Agentic Phase 7: ONE real chat turn on the configured model (orchestrator + Store
Insight on NVIDIA NIM), MCP in-process as ric_mcp on the seeded TEST database.
Skipped unless AGENT_LIVE_TEST=true.

    uv run pytest tests/agentic/test_chat_live.py -v -s
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.chat.schemas import TurnRequest
from app.agentic.chat.turn import FAILED, build_chat_deps, run_turn
from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.clock import FixedClock
from app.core.config import get_settings
from app.models import AgentChatMessage, AgentChatSession, AgentLlmCall, AgentToolCall
from mcp_server.app import InProcessServer
from mcp_server.runtime import create_mcp_engine, create_runtime
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

LIVE = get_agentic_settings()
pytestmark = pytest.mark.skipif(
    not LIVE.agent_live_test or LIVE.agent_llm_provider == "fake",
    reason="set AGENT_LIVE_TEST=true (and a real AGENT_LLM_PROVIDER) to call the model",
)


async def test_one_live_turn(itsm_engine: AsyncEngine, migrated_db: str) -> None:
    if LIVE.mcp_database_url is None:
        pytest.skip("MCP_DATABASE_URL is not set in .env")
    settings = LIVE.model_copy(
        update={"agent_user_context_token_secret": SecretStr("live-chat-" + "x" * 32)}
    )
    url = make_url(settings.mcp_database_url).set(database=make_url(migrated_db).database)
    mcp_engine = create_mcp_engine(settings, url.render_as_string(hide_password=False))
    clock = FixedClock(ITSM_ANCHOR)
    try:
        async with AsyncSession(itsm_engine) as db:
            await write_store_edges(db, TEMPLATE)
            await db.commit()
            store = await store_id_for_site(db, "01234")
        runtime = create_runtime(settings, None, mcp_engine, clock=clock, with_embedder=False)
        async with InProcessServer(runtime) as server:
            deps = build_chat_deps(
                settings, get_settings(), itsm_engine, CATALOGUE, runtime.policy, server.url,
                clock=clock,
            )  # fmt: skip
            response = await run_turn(
                deps,
                TurnRequest(
                    store_id=store, user_id="live.manager", role="store_manager",
                    text="Why are card payments slow today?", client_message_id=uuid.uuid4(),
                ),
            )  # fmt: skip
        print(response.model_dump_json(indent=2))
        assert response.answer and response.answer != FAILED, response.warnings
        assert response.pending_action is None
    finally:
        await mcp_engine.dispose()
        async with AsyncSession(itsm_engine) as db:
            for model in (AgentToolCall, AgentLlmCall, AgentChatMessage, AgentChatSession):
                await db.execute(delete(model))
            await db.commit()
