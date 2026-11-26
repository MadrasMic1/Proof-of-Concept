# tests/agentic/test_mcp_e2e.py
"""Agentic Phase 5 end to end: the MCP server in this process (127.0.0.1, random port),
logged in as ric_mcp on the TEST database, called over Streamable HTTP with signed
tokens exactly as an agent will. Data is committed (the server has its own
connections), so everything this test adds is deleted at the end; the seeded rows
are removed by conftest's itsm_engine. Skipped when MCP_DATABASE_URL is not set.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.common.tokens import mint_token
from app.agentic.config import get_agentic_settings
from app.agentic.kb.articles import load_articles
from app.agentic.kb.ingest import publish_articles
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.clock import FixedClock
from app.models import (
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbArticle,
    AgentKbChunk,
    AgentKbGuideSession,
    AgentToolCall,
)
from mcp_server.app import InProcessServer, envelope_from_result
from mcp_server.runtime import create_mcp_engine, create_runtime
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

SETTINGS = get_agentic_settings().model_copy(
    update={"agent_user_context_token_secret": SecretStr("e2e-secret-" + "x" * 32)}
)


async def _cleanup(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        for model in (
            AgentToolCall,
            AgentIncidentDraft,
            AgentKbGuideSession,
            AgentChatSession,
            AgentKbChunk,
            AgentKbArticle,
        ):
            await db.execute(delete(model))
        await db.commit()


async def _call(url: str, token: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        return envelope_from_result(await client.call_tool(tool, args))


async def test_mcp_server_over_http(itsm_engine: AsyncEngine, migrated_db: str) -> None:
    if SETTINGS.mcp_database_url is None:
        pytest.skip("MCP_DATABASE_URL is not set in .env")
    test_db = make_url(migrated_db).database
    mcp_url = make_url(SETTINGS.mcp_database_url).set(database=test_db)
    mcp_engine = create_mcp_engine(SETTINGS, mcp_url.render_as_string(hide_password=False))
    try:
        async with AsyncSession(itsm_engine) as db:
            await write_store_edges(db, TEMPLATE)
            articles, _ = load_articles(ARTICLES)
            await publish_articles(db, articles, CATALOGUE, embedder=None, now=ITSM_ANCHOR)
            paris = await store_id_for_site(db, "02556")
            manchester = await store_id_for_site(db, "03340")
            chat = uuid.uuid4()
            await db.execute(
                insert(AgentChatSession).values(
                    id=chat, store_id=manchester, user_id="mgr", role="store_manager"
                )
            )
            await db.commit()
        runtime = create_runtime(
            SETTINGS, None, mcp_engine, clock=FixedClock(ITSM_ANCHOR), with_embedder=False
        )

        def token(store: uuid.UUID, agent: str, role: str = "store_manager", **kw: Any) -> str:
            return mint_token(
                SETTINGS,
                user_id="mgr",
                role=role,
                store_id=store,
                agent=agent,
                correlation_id=uuid.uuid4(),
                policy_version=kw.pop("policy_version", "1"),
                **kw,
            )

        async with InProcessServer(runtime) as server:
            url = server.url
            findings = await _call(url, token(paris, "store_insight"), "store_get_findings", {})
            assert findings["ok"], findings
            assert findings["data"]["findings"][0]["root_node"]["name"] == "SW02556N303"

            forged = mint_token(
                SETTINGS.model_copy(
                    update={"agent_user_context_token_secret": SecretStr("f" * 40)}
                ),
                user_id="mgr", role="store_manager", store_id=paris, agent="store_insight",
                correlation_id=uuid.uuid4(), policy_version="1",
            )  # fmt: skip
            refused = await _call(url, forged, "store_get_findings", {})
            assert refused["error"]["code"] == "invalid_token"

            wrong_agent = await _call(
                url, token(paris, "store_insight"), "kb_find_articles", {"fault_code": "X"}
            )
            assert wrong_agent["error"]["code"] == "forbidden"
            staff_draft = await _call(
                url,
                token(manchester, "incident_desk", role="store_staff", chat_session_id=chat),
                "itsm_prepare_draft",
                {"short_description": "x", "description": "y", "impact": 3, "urgency": 3},
            )
            assert staff_draft["error"]["code"] == "forbidden"
            mismatch = await _call(
                url, token(paris, "store_insight", policy_version="99"), "store_get_findings", {}
            )
            assert mismatch["error"]["code"] == "policy_mismatch"
            bad_input = await _call(
                url, token(paris, "store_insight"), "store_resolve_device", {"text": ""}
            )
            assert bad_input["error"]["code"] == "invalid_input"

            guide = await _call(
                url,
                token(manchester, "kb_guide", chat_session_id=chat),
                "kb_guide_start",
                {"kb_ref": "KB-RF-001"},
            )
            assert guide["ok"], guide
            assert "data.step.instruction" in guide["untrusted_fields"]

            manager = token(manchester, "incident_desk", chat_session_id=chat)
            found = await _call(url, token(manchester, "store_insight"), "store_get_findings", {})
            gun = next(
                f for f in found["data"]["findings"] if f["root_node"]["name"] == "RF Gun 02"
            )
            draft = await _call(
                url,
                manager,
                "itsm_prepare_draft",
                {
                    "short_description": "RF Gun 02 - battery does not charge",
                    "description": "RF Gun 02 shows low battery and does not charge.",
                    "impact": 3,
                    "urgency": 3,
                    "finding_id": gun["finding_id"],
                },
            )
            assert draft["ok"] and draft["data"]["outcome"] == "draft_ready", draft

            async with httpx.AsyncClient() as http:
                health = await http.get(f"{server.base}/healthz")
            if health.status_code != 404:
                assert health.json()["tools"] == 19 and health.json()["database"] == "ok"

        async with AsyncSession(itsm_engine) as db:
            outcomes = dict(
                (
                    await db.execute(
                        select(AgentToolCall.outcome, func.count()).group_by(AgentToolCall.outcome)
                    )
                ).all()
            )
            drafts = await db.scalar(select(func.count()).select_from(AgentIncidentDraft))
        assert outcomes.get("ok", 0) >= 4 and outcomes.get("forbidden", 0) >= 2
        assert outcomes.get("invalid_token") == 1 and outcomes.get("policy_mismatch") == 1
        assert drafts == 1
    finally:
        await mcp_engine.dispose()
        await _cleanup(itsm_engine)
