# tests/agentic/test_chat_turns_db.py
"""Agentic Phase 7: whole chat turns with a SCRIPTED model - the real turn service,
orchestrator, specialists, MCP server (in-process, ric_mcp) on the seeded TEST database.
One scripted model answers every request in order (orchestrator and specialists).
Everything added is deleted at the end. Skipped when MCP_DATABASE_URL is not set."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.chat.composer import NOTHING_RAISED
from app.agentic.chat.schemas import AssistantResponse, TurnRequest
from app.agentic.chat.turn import build_chat_deps, run_turn
from app.agentic.config import get_agentic_settings
from app.agentic.kb.articles import load_articles
from app.agentic.kb.ingest import publish_articles
from app.agentic.llm.client import ModelTarget
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph.seed_db import write_store_edges
from app.core.clock import FixedClock
from app.core.config import get_settings
from app.models import (
    AgentChatMessage,
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
from tests.agentic.agent_script import ScriptedModel, Step, call, last_tool_result
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

SETTINGS = get_agentic_settings().model_copy(
    update={"agent_user_context_token_secret": SecretStr("chat-secret-" + "x" * 32)}
)
TARGET = ModelTarget("fake", "scripted-model", "http://fake.local/v1", "fake", 30)


async def _cleanup(engine: AsyncEngine) -> None:
    async with AsyncSession(engine) as db:
        for model in (
            AgentToolCall,
            AgentLlmCall,
            AgentChatMessage,
            AgentIncidentDraft,
            AgentKbGuideSession,
            AgentChatSession,
            AgentKbChunk,
            AgentKbArticle,
        ):
            await db.execute(delete(model))
        await db.commit()


def task_of(body: dict[str, Any]) -> dict[str, Any]:
    """The specialist's task JSON (its first user message)."""
    for message in body.get("messages", []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(part.get("text", "") for part in content)
            return json.loads(content)
    raise AssertionError("no user message")


def finding_for(root: str) -> Step:
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


def start_best(body: dict[str, Any]) -> dict[str, Any]:
    ref = last_tool_result(body)["data"]["candidates"][0]["kb_ref"]
    return {"tool": "kb_guide_start", "args": {"kb_ref": ref}}


def submit_guide(body: dict[str, Any]) -> dict[str, Any]:
    guide = last_tool_result(body)["data"]
    return {
        "tool": "submit_result",
        "args": {
            "outcome": "needs_user_input",
            "summary": "Next.",
            "guide_id": guide["guide_id"],
        },
    }


def get_state(body: dict[str, Any]) -> dict[str, Any]:
    return {"tool": "kb_guide_get_state", "args": {"guide_id": task_of(body)["guide_id"]}}


def record(outcome: str) -> Step:
    def step(body: dict[str, Any]) -> dict[str, Any]:
        state = last_tool_result(body)["data"]
        return {
            "tool": "kb_guide_record_outcome",
            "args": {
                "guide_id": task_of(body)["guide_id"],
                "step_id": state["step"]["step_id"],
                "outcome": outcome,
                "state_version": state["state_version"],
            },
        }

    return step


def answer(text: str, *cards: str) -> Step:
    return call("submit_answer", answer=text, show_cards=list(cards))


async def test_chat_journeys(itsm_engine: AsyncEngine, migrated_db: str) -> None:
    if SETTINGS.mcp_database_url is None:
        pytest.skip("MCP_DATABASE_URL is not set in .env")
    test_db = make_url(migrated_db).database
    mcp_url = make_url(SETTINGS.mcp_database_url).set(database=test_db)
    mcp_engine = create_mcp_engine(SETTINGS, mcp_url.render_as_string(hide_password=False))
    clock = FixedClock(ITSM_ANCHOR)
    try:
        async with AsyncSession(itsm_engine) as db:
            await write_store_edges(db, TEMPLATE)
            articles, _ = load_articles(ARTICLES)
            await publish_articles(db, articles, CATALOGUE, embedder=None, now=ITSM_ANCHOR)
            await db.commit()
            manchester = await store_id_for_site(db, "03340")
            chennai = await store_id_for_site(db, "01234")
        runtime = create_runtime(SETTINGS, None, mcp_engine, clock=clock, with_embedder=False)
        async with InProcessServer(runtime) as server:

            async def turn(
                model: ScriptedModel,
                store: uuid.UUID,
                user: str,
                role: str,
                text: str,
                client: uuid.UUID | None = None,
            ) -> AssistantResponse:
                deps = build_chat_deps(
                    SETTINGS, get_settings(), itsm_engine, CATALOGUE, runtime.policy,
                    server.url, inner=model.transport(), targets=[TARGET], clock=clock,
                )  # fmt: skip
                request = TurnRequest(
                    store_id=store, user_id=user, role=role, text=text,
                    client_message_id=client or uuid.uuid4(),
                )  # fmt: skip
                response = await run_turn(deps, request)
                assert model.steps == [], f"unused scripted steps after: {text}"
                return response

            # --- A: store staff, RF Gun 02 battery: diagnose -> guide -> exhausted -> manager
            ravi = (manchester, "ravi", "store_staff")
            model = ScriptedModel(
                [
                    call("ask_store_insight", question="scanner keeps dying",
                         devices_mentioned=["rf gun 2"]),
                    call("store_get_findings"),
                    finding_for("RF Gun 02"),
                    call("ask_kb_guide", mode="start", issue="RF gun battery"),
                    call("kb_find_articles", element_type="rf_gun", fault_code="BATTERY_LOW"),
                    start_best,
                    submit_guide,
                    answer("RF Gun 02's battery is low. Let's fix it.", "finding", "kb_step"),
                ]
            )  # fmt: skip
            first = await turn(model, *ravi, "my scanner keeps dying")
            assert first.state == "guiding", first.warnings
            assert [c.kind for c in first.cards] == ["finding", "kb_step"]
            step = first.cards[1].data
            assert (step["kb_ref"], step["step_id"]) == ("KB-RF-001", "S1")
            assert "No charging light" in first.suggested_replies
            assert first.pending_action is None

            model = ScriptedModel(
                [
                    call("ask_kb_guide", mode="continue", user_report="no charging light"),
                    get_state,
                    record("not_charging"),
                    submit_guide,
                    answer("Let's check the battery contacts."),
                ]
            )
            second = await turn(model, *ravi, "no charging light")
            assert second.state == "guiding"
            assert second.cards[0].data["step_id"] == "S3"

            model = ScriptedModel(
                [
                    call("ask_kb_guide", mode="continue", user_report="still no light"),
                    get_state,
                    record("not_charging"),
                    submit_guide,
                    answer("It is not fixed. Please ask your store manager to raise it."),
                ]
            )
            third = await turn(model, *ravi, "still no light")
            assert third.state == "guide_exhausted" and third.cards == []

            model = ScriptedModel(
                [
                    call("ask_incident_desk", mode="draft", issue="RF Gun 02 battery"),
                    answer("Only your store manager can raise an incident."),
                ]
            )
            fourth = await turn(model, *ravi, "please raise a ticket")
            assert fourth.state == "idle"
            async with AsyncSession(itsm_engine) as db:
                drafts = await db.scalar(select(func.count()).select_from(AgentIncidentDraft))
            assert drafts == 0

            # --- B: manager, WAN failover covered by INC00001234 (no ticket offered)
            model = ScriptedModel(
                [
                    call("ask_store_insight", question="card payments are slow"),
                    call("store_get_findings"),
                    finding_for("WAN01234-PRI"),
                    answer("The main internet link is down; Network Ops is on it.", "finding"),
                ]
            )
            wan = await turn(model, chennai, "priya", "store_manager", "why are payments slow")
            [card] = wan.cards
            assert card.data["outcome"] == "covered_by_incident"
            assert card.data["causes"][0]["covered_by_incident"] == "INC00001234"
            assert wan.pending_action is None and wan.state == "idle"

            # --- C: manager raises RF Gun 02; a false "raised" claim is replaced; replay
            def duplicates(body: dict[str, Any]) -> dict[str, Any]:
                finding = task_of(body)["task"]["finding_id"]
                return {
                    "tool": "itsm_check_duplicates",
                    "args": {"finding_id": finding, "text": "RF Gun 02 battery"},
                }

            def prepare(body: dict[str, Any]) -> dict[str, Any]:
                return {
                    "tool": "itsm_prepare_draft",
                    "args": {
                        "short_description": "RF Gun 02 - battery does not charge",
                        "description": "RF Gun 02 shows low battery and does not charge.",
                        "impact": 3,
                        "urgency": 3,
                        "finding_id": task_of(body)["task"]["finding_id"],
                    },
                }

            def submit_draft(body: dict[str, Any]) -> dict[str, Any]:
                draft = last_tool_result(body)["data"]["draft"]
                return {
                    "tool": "submit_result",
                    "args": {"outcome": "draft_ready", "summary": "Draft ready.",
                             "draft_id": draft["draft_id"]},
                }  # fmt: skip

            model = ScriptedModel(
                [
                    call("ask_store_insight", question="rf gun 2 battery keeps dying"),
                    call("store_get_findings"),
                    finding_for("RF Gun 02"),
                    call("ask_incident_desk", mode="draft", issue="RF Gun 02 battery",
                         user_words="battery keeps dying"),
                    duplicates,
                    prepare,
                    submit_draft,
                    answer("I have raised incident INC00009999 for you.", "incident_draft"),
                ]
            )  # fmt: skip
            client = uuid.uuid4()
            mgr = (manchester, "mgr", "store_manager")
            drafted = await turn(model, *mgr, "rf gun 2 keeps dying, raise a ticket", client)
            assert drafted.state == "draft_pending", drafted.warnings
            assert drafted.answer == NOTHING_RAISED
            [card] = drafted.cards
            assert card.kind == "incident_draft"
            assert drafted.pending_action is not None
            assert drafted.pending_action.draft_id == card.data["draft_id"]

            replay = await turn(ScriptedModel([]), *mgr, "rf gun 2 keeps dying", client)
            assert replay.replayed and replay.turn_no == drafted.turn_no
            assert replay.pending_action == drafted.pending_action

        async with AsyncSession(itsm_engine) as db:
            sessions = await db.scalar(select(func.count()).select_from(AgentChatSession))
            messages = await db.scalar(select(func.count()).select_from(AgentChatMessage))
            orchestrator_calls = await db.scalar(
                select(func.count())
                .select_from(AgentLlmCall)
                .where(AgentLlmCall.agent == "orchestrator")
            )
        assert sessions == 3 and messages == 2 * 6
        assert orchestrator_calls and orchestrator_calls >= 12
    finally:
        await mcp_engine.dispose()
        await _cleanup(itsm_engine)
