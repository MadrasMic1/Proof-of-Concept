# tests/agentic/test_agent_live.py
"""Agentic Phase 6: ONE real Store Insight run on the configured model (NVIDIA NIM),
with the MCP server in-process as ric_mcp on the seeded TEST database.
Skipped unless AGENT_LIVE_TEST=true (and AGENT_LLM_PROVIDER is a real provider).

    uv run pytest tests/agentic/test_agent_live.py -v -s
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import SecretStr
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.agents.deps import build_agent_deps
from app.agentic.agents.runtime import run_specialist
from app.agentic.agents.schemas import StoreInsightBrief, TrustedContext
from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.clock import FixedClock
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.models import AgentLlmCall, AgentToolCall
from mcp_server.app import InProcessServer
from mcp_server.runtime import create_mcp_engine, create_runtime
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

LIVE = get_agentic_settings()
pytestmark = pytest.mark.skipif(
    not LIVE.agent_live_test or LIVE.agent_llm_provider == "fake",
    reason="set AGENT_LIVE_TEST=true (and a real AGENT_LLM_PROVIDER) to call the model",
)


async def test_store_insight_live(itsm_engine: AsyncEngine, migrated_db: str) -> None:
    if LIVE.mcp_database_url is None:
        pytest.skip("MCP_DATABASE_URL is not set in .env")
    settings = LIVE.model_copy(
        update={"agent_user_context_token_secret": SecretStr("live-secret-" + "x" * 32)}
    )
    test_db = make_url(migrated_db).database
    url = make_url(settings.mcp_database_url).set(database=test_db)
    mcp_engine = create_mcp_engine(settings, url.render_as_string(hide_password=False))
    try:
        async with AsyncSession(itsm_engine) as db:
            await write_store_edges(db, TEMPLATE)
            store = await store_id_for_site(db, "01234")
            await db.commit()
        runtime = create_runtime(
            settings, None, mcp_engine, clock=FixedClock(ITSM_ANCHOR), with_embedder=False
        )
        async with InProcessServer(runtime) as server:
            deps = build_agent_deps(
                settings,
                get_settings(),
                runtime.policy,
                server.url,
                usage_factory=create_session_factory(itsm_engine),
            )
            result = await run_specialist(
                "store_insight",
                StoreInsightBrief(question="Why are card payments slow today?"),
                TrustedContext("live.test", "store_manager", store, uuid.uuid4()),
                deps,
            )
        print(result.model_dump_json(indent=2))
        assert result.status == "ok", result.problems
        assert result.outcome in ("covered_by_incident", "cause_found")
        assert any(c.tool == "store_get_findings" for c in result.trace.tool_calls)
    finally:
        await mcp_engine.dispose()
        async with AsyncSession(itsm_engine) as db:
            await db.execute(delete(AgentToolCall))
            await db.execute(delete(AgentLlmCall))
            await db.commit()
