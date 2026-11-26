# tests/agentic/test_agent_specialists_db.py
"""Agentic Phase 6: the three specialists end to end with a SCRIPTED model - the real
Agent Framework loop, the real bridge, the real MCP server in-process as ric_mcp on the
seeded TEST database. Data is committed (the server has its own connections), so
everything this test adds is deleted at the end. Skipped when MCP_DATABASE_URL is not set.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, insert, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.agents.deps import build_agent_deps
from app.agentic.agents.runtime import run_specialist
from app.agentic.agents.schemas import (
    IncidentDeskBrief,
    KbGuideBrief,
    StoreInsightBrief,
    TrustedContext,
)
from app.agentic.config import get_agentic_settings
from app.agentic.kb.articles import load_articles
from app.agentic.kb.ingest import publish_articles
from app.agentic.llm.client import ModelTarget
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.clock import FixedClock
from app.core.config import get_settings
from app.db.session import create_session_factory
from app.models import (
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbArticle,
    AgentKbChunk,
    AgentKbGuideSession,
    AgentLlmCall,
    AgentToolCall,
)
from mcp_server.app import InProcessServer
from mcp_server.runtime import create_mcp_engine, create_runtime
from tests.agentic.agent_script import ScriptedModel, call, last_tool_result
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

SETTINGS = get_agentic_settings().model_copy(
    update={"agent_user_context_token_secret": SecretStr("agents-secret-" + "x" * 32)}
)
TARGET = ModelTarget("fake", "scripted-model", "http://fake.local/v1", "fake", 30)


async def _cleanup(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        for model in (
            AgentToolCall,
            AgentLlmCall,
            AgentIncidentDraft,
            AgentKbGuideSession,
            AgentChatSession,
            AgentKbChunk,
            AgentKbArticle,
        ):
            await db.execute(delete(model))
        await db.commit()


def _finding_for(root: str) -> Any:
    def step(body: dict[str, Any]) -> dict[str, Any]:
        findings = last_tool_result(body)["data"]["findings"]
        finding = next(f for f in findings if f["root_node"]["name"] == root)
        return {
            "tool": "submit_result",
            "args": {
                "outcome": "cause_found",
                "summary": f"{root} explains it.",
                "finding_ids": [finding["finding_id"]],
            },
        }

    return step


async def test_specialists_with_a_scripted_model(
    itsm_engine: AsyncEngine, migrated_db: str
) -> None:
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
            chennai = await store_id_for_site(db, "01234")
            bengaluru = await store_id_for_site(db, "01456")
            manchester = await store_id_for_site(db, "03340")
            staff_chat, manager_chat = uuid.uuid4(), uuid.uuid4()
            await db.execute(
                insert(AgentChatSession).values(
                    id=staff_chat, store_id=bengaluru, user_id="staff", role="store_staff"
                )
            )
            await db.execute(
                insert(AgentChatSession).values(
                    id=manager_chat, store_id=manchester, user_id="mgr", role="store_manager"
                )
            )
            await db.commit()
        runtime = create_runtime(
            SETTINGS, None, mcp_engine, clock=FixedClock(ITSM_ANCHOR), with_embedder=False
        )
        usage = create_session_factory(itsm_engine)
        async with InProcessServer(runtime) as server:

            def deps(model: ScriptedModel) -> Any:
                return build_agent_deps(
                    SETTINGS, get_settings(), runtime.policy, server.url,
                    usage_factory=usage, inner=model.transport(), targets=[TARGET],
                )  # fmt: skip

            def trusted(store: uuid.UUID, role: str, user: str, **kw: Any) -> TrustedContext:
                return TrustedContext(user, role, store, uuid.uuid4(), **kw)

            # Store Insight: WAN failover in 01234, covered by INC00001234.
            model = ScriptedModel([call("store_get_findings"), _finding_for("WAN01234-PRI")])
            result = await run_specialist(
                "store_insight",
                StoreInsightBrief(question="why are card payments slow"),
                trusted(chennai, "store_manager", "mgr"),
                deps(model),
            )
            assert result.status == "ok", result.problems
            assert (result.outcome, result.suggested_next) == (
                "covered_by_incident",
                "check_incident",
            )
            assert result.body["causes"][0]["covered_by_incident"] == "INC00001234"
            assert [c.tool for c in result.trace.tool_calls] == ["store_get_findings"]
            assert "submit_result" in str(model.requests[0]["tools"])

            # Store Insight in 03340: an invented finding is rejected, the repair passes.
            invented = call(
                "submit_result", outcome="cause_found", summary="x", finding_ids=["F-deadbeef"]
            )
            model = ScriptedModel([invented, call("store_get_findings"), _finding_for("RF Gun 02")])
            result = await run_specialist(
                "store_insight",
                StoreInsightBrief(question="scanner keeps dying", devices_mentioned=["rf gun 2"]),
                trusted(manchester, "store_manager", "mgr"),
                deps(model),
            )
            assert result.status == "ok" and result.trace.attempts == 2
            assert result.suggested_next == "guide"
            gun = result.body["causes"][0]
            assert gun["staff_can_fix"] and gun["kb_tags"] == ["rf-battery"]

            # KB Guide: start, then continue, in the staff chat of 01456.
            def start_with_best(body: dict[str, Any]) -> dict[str, Any]:
                ref = last_tool_result(body)["data"]["candidates"][0]["kb_ref"]
                return {"tool": "kb_guide_start", "args": {"kb_ref": ref}}

            def submit_guide(body: dict[str, Any]) -> dict[str, Any]:
                guide = last_tool_result(body)["data"]
                return {
                    "tool": "submit_result",
                    "args": {
                        "outcome": "needs_user_input",
                        "summary": "First step.",
                        "guide_id": guide["guide_id"],
                    },
                }

            staff = trusted(bengaluru, "store_staff", "staff", chat_session_id=staff_chat)
            model = ScriptedModel(
                [
                    call("kb_find_articles", element_type="printer", fault_code="PAPER_JAM"),
                    start_with_best,
                    submit_guide,
                ]
            )
            started = await run_specialist(
                "kb_guide",
                KbGuideBrief(mode="start", element_type="printer", fault_code="PAPER_JAM"),
                staff,
                deps(model),
            )
            assert started.status == "ok", started.problems
            step = started.body["step"]
            assert (step["kb_ref"], step["step_id"]) == ("KB-PRN-001", "S1")
            assert "untrusted" not in step["instruction"]
            guide_id = started.body["guide_id"]

            def record(body: dict[str, Any]) -> dict[str, Any]:
                state = last_tool_result(body)["data"]
                return {
                    "tool": "kb_guide_record_outcome",
                    "args": {
                        "guide_id": guide_id,
                        "step_id": state["step"]["step_id"],
                        "outcome": "jam_seen",
                        "state_version": state["state_version"],
                        "reply": "yes paper was stuck",
                    },
                }

            model = ScriptedModel(
                [call("kb_guide_get_state", guide_id=guide_id), record, submit_guide]
            )
            moved = await run_specialist(
                "kb_guide",
                KbGuideBrief(mode="continue", user_report="yes paper was stuck"),
                trusted(
                    bengaluru,
                    "store_staff",
                    "staff",
                    chat_session_id=staff_chat,
                    guide_id=uuid.UUID(guide_id),
                ),  # fmt: skip
                deps(model),
            )
            assert moved.status == "ok" and moved.body["step"]["step_id"] == "S2"

            # Incident Desk: a draft for RF Gun 02 in the manager chat of 03340.
            def prepare(body: dict[str, Any]) -> dict[str, Any]:
                return {
                    "tool": "itsm_prepare_draft",
                    "args": {
                        "short_description": "RF Gun 02 - battery does not charge",
                        "description": "RF Gun 02 shows low battery and does not charge.",
                        "impact": 3,
                        "urgency": 3,
                        "finding_id": gun["finding_id"],
                    },
                }

            def submit_draft(body: dict[str, Any]) -> dict[str, Any]:
                draft = last_tool_result(body)["data"]["draft"]
                return {
                    "tool": "submit_result",
                    "args": {
                        "outcome": "draft_ready",
                        "summary": "Draft ready.",
                        "draft_id": draft["draft_id"],
                    },
                }

            model = ScriptedModel(
                [
                    call("itsm_check_duplicates", finding_id=gun["finding_id"], text="RF Gun 02"),
                    prepare,
                    submit_draft,
                ]
            )
            drafted = await run_specialist(
                "incident_desk",
                IncidentDeskBrief(
                    mode="draft", issue="scanner battery", finding_id=gun["finding_id"]
                ),
                trusted(manchester, "store_manager", "mgr", chat_session_id=manager_chat),
                deps(model),
            )
            assert drafted.status == "ok", drafted.problems
            assert (drafted.outcome, drafted.suggested_next) == ("draft_ready", "show_draft")
            assert drafted.body["draft"]["affected"]["name"] == "RF Gun 02"

        async with AsyncSession(itsm_engine) as db:
            calls = await db.scalar(select(func.count()).select_from(AgentLlmCall))
            tool_rows = await db.scalar(select(func.count()).select_from(AgentToolCall))
        assert calls and calls >= 11 and tool_rows and tool_rows >= 8
    finally:
        await mcp_engine.dispose()
        await _cleanup(itsm_engine)
