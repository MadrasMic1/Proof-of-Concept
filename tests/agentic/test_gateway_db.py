# tests/agentic/test_gateway_db.py
"""Agentic Phase 8: the chat API over HTTP (conftest app + client, test database) with a
SCRIPTED model, the MCP server in-process as ric_mcp and a FAKE GLPI client (as
tests/test_store_reply_itsm.py). Covers messages, replay, the session view, Confirm with
GLPI down (nothing created, draft stays pending) then up (RIC incident + GLPI ticket with
location, group, category, CI and external id), a double tap, a stale version, a
duplicate raised between drafting and Confirm, Cancel, a store staff user, an unknown
store and the switch. Everything added is deleted at the end; incidents, audit rows and
the glpi_* link values are removed with the seeded rows by conftest's itsm_engine."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, Response
from pydantic import SecretStr
from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.agentic.chat.turn import build_chat_deps
from app.agentic.config import get_agentic_settings
from app.agentic.gateway.routes import get_chat_deps
from app.agentic.kb.articles import load_articles
from app.agentic.kb.ingest import publish_articles
from app.agentic.llm.client import ModelTarget
from app.agentic.store_graph.seed_db import write_store_edges
from app.api.deps import get_itsm_client
from app.core.clock import FixedClock
from app.core.config import get_settings
from app.itsm.errors import ItsmUnavailable
from app.models import (
    AgentChatMessage,
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbArticle,
    AgentKbChunk,
    AgentKbGuideSession,
    AgentLlmCall,
    AgentToolCall,
    AssignmentGroup,
    AuditLog,
    Device,
    Incident,
    Store,
)
from mcp_server.app import InProcessServer
from mcp_server.runtime import create_mcp_engine, create_runtime
from tests.agentic.agent_script import ScriptedModel, Step, call, last_tool_result
from tests.agentic.test_chat_turns_db import answer, finding_for, task_of
from tests.agentic.test_kb_articles import ARTICLES
from tests.agentic.test_store_scenarios import CATALOGUE
from tests.agentic.test_store_snapshot_db import TEMPLATE
from tests.conftest import ITSM_ANCHOR

SETTINGS = get_agentic_settings().model_copy(
    update={
        "agent_user_context_token_secret": SecretStr("gateway-secret-" + "x" * 32),
        "agent_dev_user_roles": "mgr=store_manager,mgr2=store_manager,boss=store_manager",
        "agent_enabled": True,
        "agent_confirm_to_glpi": True,
    }
)
GLPI_LOCATION, GLPI_GROUP, GLPI_ASSET = 990340, 990041, 990777
TARGET = ModelTarget("fake", "scripted-model", "http://fake.local/v1", "fake", 30)
BASE = "/api/v1/chat"


class FakeGlpi:
    """The ticket part of GlpiClient; error set = GLPI is down."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.created: list[dict[str, Any]] = []
        self.linked: list[tuple[int, str, int]] = []

    async def find_category_id(self, category: str, subcategory: str | None) -> int | None:
        if self.error is not None:
            raise self.error
        return 11 if (category, subcategory) == ("hardware", "RF Gun") else None

    async def create_ticket(self, fields: dict[str, Any]) -> int:
        if self.error is not None:
            raise self.error
        self.created.append(fields)
        return 990000 + len(self.created)

    async def link_item(self, ticket_id: int, itemtype: str, item_id: int) -> None:
        self.linked.append((ticket_id, itemtype, item_id))


def headers(site: str, user: str) -> dict[str, str]:
    return {"X-Site-Id": site, "X-User": user}


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


def draft_turn(acknowledge: bool = False) -> list[Step]:
    """Orchestrator: diagnose RF Gun 02, then Incident Desk drafts it."""

    def duplicates(body: dict[str, Any]) -> dict[str, Any]:
        return {
            "tool": "itsm_check_duplicates",
            "args": {"finding_id": task_of(body)["task"]["finding_id"], "text": "RF Gun 02"},
        }

    def prepare(body: dict[str, Any]) -> dict[str, Any]:
        task = task_of(body)["task"]
        return {
            "tool": "itsm_prepare_draft",
            "args": {
                "short_description": "RF Gun 02 - battery does not charge",
                "description": "RF Gun 02 shows low battery and does not charge.",
                "impact": 3,
                "urgency": 3,
                "finding_id": task["finding_id"],
                "acknowledge_possible_duplicates": task.get(
                    "acknowledge_possible_duplicates", False
                ),
            },
        }

    def submit_draft(body: dict[str, Any]) -> dict[str, Any]:
        draft = last_tool_result(body)["data"]["draft"]
        return {
            "tool": "submit_result",
            "args": {"outcome": "draft_ready", "summary": "Draft ready.",
                     "draft_id": draft["draft_id"]},
        }  # fmt: skip

    return [
        call("ask_store_insight", question="rf gun 2 battery keeps dying"),
        call("store_get_findings"),
        finding_for("RF Gun 02"),
        call("ask_incident_desk", mode="draft", issue="RF Gun 02 battery",
             user_words="battery keeps dying",
             manager_accepted_possible_duplicate=acknowledge),
        duplicates,
        prepare,
        submit_draft,
        answer("Here is the draft. Tap Confirm to raise it.", "incident_draft"),
    ]  # fmt: skip


async def test_chat_api_and_confirm(
    itsm_engine: AsyncEngine, migrated_db: str, app: FastAPI, client: AsyncClient
) -> None:
    if SETTINGS.mcp_database_url is None:
        pytest.skip("MCP_DATABASE_URL is not set in .env")
    url = make_url(SETTINGS.mcp_database_url).set(database=make_url(migrated_db).database)
    mcp_engine = create_mcp_engine(SETTINGS, url.render_as_string(hide_password=False))
    clock = FixedClock(ITSM_ANCHOR)
    holder: dict[str, Any] = {}
    glpi = FakeGlpi()
    app.dependency_overrides[get_chat_deps] = lambda: holder["deps"]
    app.dependency_overrides[get_itsm_client] = lambda: glpi
    try:
        async with AsyncSession(itsm_engine) as db:
            await write_store_edges(db, TEMPLATE)
            articles, _ = load_articles(ARTICLES)
            await publish_articles(db, articles, CATALOGUE, embedder=None, now=ITSM_ANCHOR)
            await db.execute(
                update(Store)
                .where(Store.site_code == "03340")
                .values(glpi_location_id=GLPI_LOCATION)
            )
            await db.execute(
                update(AssignmentGroup)
                .where(AssignmentGroup.name == "Retail EUC Ops")
                .values(glpi_group_id=GLPI_GROUP)
            )
            manchester = select(Store.id).where(Store.site_code == "03340").scalar_subquery()
            await db.execute(
                update(Device)
                .where(Device.store_id == manchester, Device.name == "RF Gun 02")
                .values(glpi_itemtype="Peripheral", glpi_item_id=GLPI_ASSET)
            )
            await db.commit()
        runtime = create_runtime(SETTINGS, None, mcp_engine, clock=clock, with_embedder=False)
        async with InProcessServer(runtime) as server:

            def use(model: ScriptedModel, settings: Any = SETTINGS) -> ScriptedModel:
                holder["deps"] = build_chat_deps(
                    settings, get_settings(), app.state.engine, CATALOGUE, runtime.policy,
                    server.url, inner=model.transport(), targets=[TARGET], clock=clock,
                )  # fmt: skip
                return model

            async def say(
                site: str, user: str, text: str, client_id: str | None = None
            ) -> dict[str, Any]:
                body = {"text": text, "clientMessageId": client_id or str(uuid.uuid4())}
                response = await client.post(
                    f"{BASE}/messages", json=body, headers=headers(site, user)
                )
                assert response.status_code == 200, response.text
                return response.json()

            async def act(
                site: str, user: str, action: str, draft: str, version: int | None
            ) -> Response:
                body = {"action": action, "draftId": draft, "version": version}
                return await client.post(f"{BASE}/actions", json=body, headers=headers(site, user))

            # --- no chat yet; unknown store
            use(ScriptedModel([]))
            empty = (await client.get(f"{BASE}/session", headers=headers("03340", "mgr"))).json()
            assert (empty["session_id"], empty["state"]) == (None, "idle")
            assert empty["role"] == "store_manager"
            unknown = await client.post(
                f"{BASE}/messages", json={"text": "hi"}, headers=headers("99999", "mgr")
            )
            assert unknown.status_code == 404

            # --- manager drafts RF Gun 02; the same client id replays without the model
            model = use(ScriptedModel(draft_turn()))
            client_id = str(uuid.uuid4())
            drafted = await say("03340", "mgr", "rf gun 2 keeps dying, raise a ticket", client_id)
            assert model.steps == []
            assert drafted["state"] == "draft_pending", drafted["warnings"]
            pending = drafted["pending_action"]
            assert pending and pending["version"] == 1
            use(ScriptedModel([]))
            replay = await say("03340", "mgr", "rf gun 2 keeps dying, raise a ticket", client_id)
            assert replay["replayed"] and replay["pending_action"] == pending
            view = (await client.get(f"{BASE}/session", headers=headers("03340", "mgr"))).json()
            assert view["state"] == "draft_pending" and view["pending_action"] == pending
            assert [m["role"] for m in view["messages"]] == ["user", "assistant"]

            # --- a second manager drafts the same problem (accepting the possible duplicate)
            use(ScriptedModel(draft_turn(acknowledge=True)))
            second = await say("03340", "mgr2", "rf gun 2 battery, raise it anyway")
            assert second["state"] == "draft_pending", second["warnings"]
            other = second["pending_action"]

            # --- store staff cannot confirm; stale version is refused; Confirm creates once
            staff = await act("03340", "ravi", "confirm_incident", pending["draft_id"], 1)
            assert staff.status_code == 403
            stale = (await act("03340", "mgr", "confirm_incident", pending["draft_id"], 9)).json()
            assert "changed" in stale["answer"] and stale["state"] == "draft_pending"
            glpi.error = ItsmUnavailable("GLPI V1 POST Ticket: no answer within 30 s")
            down = (await act("03340", "mgr", "confirm_incident", pending["draft_id"], 1)).json()
            assert "could not be sent to GLPI" in down["answer"]
            assert down["state"] == "draft_pending" and down["pending_action"] is not None
            glpi.error = None
            done = await act("03340", "mgr", "confirm_incident", pending["draft_id"], 1)
            assert done.status_code == 200, done.text
            confirmed = done.json()
            assert confirmed["state"] == "incident_raised" and confirmed["pending_action"] is None
            number = confirmed["answer"].split()[1]
            assert number.startswith("INC") and "Retail EUC Ops" in confirmed["answer"]
            assert "(GLPI ticket 990001)" in confirmed["answer"], confirmed
            [ticket] = glpi.created
            assert ticket["externalid"] == number and ticket["type"] == 1
            assert ticket["locations_id"] == GLPI_LOCATION
            assert ticket["_groups_id_assign"] == GLPI_GROUP
            assert ticket["itilcategories_id"] == 11
            assert (ticket["urgency"], ticket["impact"]) == (2, 2)  # RIC 3 = low -> GLPI 2
            assert "RF Gun 02" in ticket["name"] and "Raised from RIC by mgr" in ticket["content"]
            assert glpi.linked == [(990001, "Peripheral", GLPI_ASSET)]
            again = (await act("03340", "mgr", "confirm_incident", pending["draft_id"], 1)).json()
            assert again["answer"] == f"Incident {number} is already raised for this."

            # --- the other manager's draft is now a duplicate: superseded, nothing created
            dup = (await act("03340", "mgr2", "confirm_incident", other["draft_id"], 1)).json()
            assert number in dup["answer"] and "no new incident" in dup["answer"]

            # --- the next turn knows the incident was raised, then the state is acknowledged
            model = use(ScriptedModel([answer(f"Incident {number} is with Retail EUC Ops.")]))
            follow = await say("03340", "mgr", "what happened with my ticket")
            assert "INCIDENT_RAISED" in str(model.requests[0]["messages"])
            assert follow["state"] == "idle"

            # --- store-level draft in 02112, then Cancel
            def prepare_inquiry(body: dict[str, Any]) -> dict[str, Any]:
                return {
                    "tool": "itsm_prepare_draft",
                    "args": {
                        "short_description": "Music system silent",
                        "description": "The in-store music has stopped.",
                        "category": "inquiry",
                        "impact": 3,
                        "urgency": 3,
                    },
                }

            def submit_inquiry(body: dict[str, Any]) -> dict[str, Any]:
                draft = last_tool_result(body)["data"]["draft"]
                return {
                    "tool": "submit_result",
                    "args": {"outcome": "draft_ready", "summary": "Draft ready.",
                             "draft_id": draft["draft_id"]},
                }  # fmt: skip

            use(
                ScriptedModel(
                    [
                        call("ask_incident_desk", mode="draft", issue="music system silent"),
                        prepare_inquiry,
                        submit_inquiry,
                        answer("Draft ready.", "incident_draft"),
                    ]
                )
            )
            music = await say("02112", "boss", "the music has stopped, raise a ticket")
            assert music["state"] == "draft_pending", music["warnings"]
            draft = music["pending_action"]
            cancel = await act("02112", "boss", "cancel_draft", draft["draft_id"], None)
            cancelled = cancel.json()
            assert cancelled["state"] == "idle" and "cancelled" in cancelled["answer"]

            # --- new conversation; the switch
            use(ScriptedModel([]))
            fresh = (
                await client.post(f"{BASE}/session/new", headers=headers("03340", "mgr"))
            ).json()
            assert fresh["session_id"] != view["session_id"] and fresh["messages"] == []
            use(ScriptedModel([]), SETTINGS.model_copy(update={"agent_enabled": False}))
            off = await say("03340", "mgr", "hello")
            assert off["state"] == "unavailable"

        async with AsyncSession(itsm_engine) as db:
            incidents = (
                (await db.execute(select(Incident).where(Incident.caller.in_(["mgr", "mgr2"]))))
                .scalars()
                .all()
            )
            statuses = dict(
                (
                    await db.execute(select(AgentIncidentDraft.user_id, AgentIncidentDraft.status))
                ).all()
            )
            audits = await db.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.entity_type == "incidents", AuditLog.source == "agent")
            )
        [incident] = incidents
        assert incident.number == number and incident.glpi_ticket_id == 990001
        assert (incident.contact_type, incident.category, incident.subcategory) == (
            "self_service",
            "hardware",
            "RF Gun",
        )
        assert incident.opened_by == "mgr" and incident.cmdb_ci_device_id is not None
        assert statuses == {"mgr": "confirmed", "mgr2": "superseded", "boss": "cancelled"}
        assert audits == 1
        assert len(glpi.created) == 1  # the superseded and cancelled drafts never reached GLPI
    finally:
        app.dependency_overrides.pop(get_chat_deps, None)
        app.dependency_overrides.pop(get_itsm_client, None)
        await mcp_engine.dispose()
        await _cleanup(itsm_engine)
