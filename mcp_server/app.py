# mcp_server/app.py
"""FastMCP wiring: one wrapper per tool (typed parameters -> the JSON schema the model
reads; docstring = the registry description), the bearer token from the HTTP header,
and GET /healthz. All logic is in app/agentic/tools (pipeline + tool functions).

Streamable HTTP, stateless (state lives in PostgreSQL), path /mcp.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import socket
from typing import Annotated, Any, Literal

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_request
from pydantic import Field
from sqlalchemy import text
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.agentic.tools.pipeline import ToolRuntime
from app.agentic.tools.registry import TOOLS

MCP_PATH = "/mcp"
SERVER_NAME = "ric-mcp"

NodeId = Annotated[str, Field(description="node id from findings or store_resolve_device")]
GuideId = Annotated[str, Field(description="guide id from kb_guide_start")]
DraftId = Annotated[str, Field(description="draft id from itsm_prepare_draft")]
Level = Annotated[int, Field(ge=1, le=3, description="1 = high, 2 = medium, 3 = low")]


def _bearer() -> str | None:
    try:
        header = get_http_request().headers.get("authorization", "")
    except Exception:
        return None
    return header.removeprefix("Bearer ").strip() or None


def build_server(runtime: ToolRuntime) -> FastMCP:
    mcp = FastMCP(SERVER_NAME)

    async def run(name: str, values: dict[str, Any]) -> dict[str, Any]:
        return await runtime.call(name, _bearer(), values)

    async def store_get_findings(
        severity_min: Annotated[
            Literal["critical", "major", "minor", "info"] | None,
            Field(description="only findings at least this severe"),
        ] = None,
        include_suspects: Annotated[
            bool, Field(description="include common-cause suspects")
        ] = True,
    ) -> dict[str, Any]:
        return await run("store_get_findings", locals())

    async def store_resolve_device(
        text: Annotated[
            str, Field(max_length=100, description="the user's own words for the device")
        ],
    ) -> dict[str, Any]:
        return await run("store_resolve_device", locals())

    async def store_get_node_status(node_id: NodeId) -> dict[str, Any]:
        return await run("store_get_node_status", locals())

    async def store_get_node_history(
        node_id: NodeId,
        hours: Annotated[
            int | None, Field(ge=1, le=24, description="look back this many hours")
        ] = None,
    ) -> dict[str, Any]:
        return await run("store_get_node_history", locals())

    async def graph_neighbourhood(
        node_id: NodeId,
        depth: Annotated[int, Field(ge=1, le=2, description="1 or 2 steps")] = 1,
    ) -> dict[str, Any]:
        return await run("graph_neighbourhood", locals())

    async def graph_impact_of(node_id: NodeId) -> dict[str, Any]:
        return await run("graph_impact_of", locals())

    async def graph_explain_path(
        from_node_id: Annotated[str, Field(description="the item with the symptom")],
        to_node_id: Annotated[str, Field(description="the suspected cause")],
    ) -> dict[str, Any]:
        return await run("graph_explain_path", locals())

    async def kb_find_articles(
        element_type: Annotated[
            str | None, Field(description="catalogue type, e.g. printer")
        ] = None,
        fault_code: Annotated[str | None, Field(description="fault code from the finding")] = None,
        kb_tags: Annotated[list[str] | None, Field(description="kb_tags from the finding")] = None,
        query: Annotated[str | None, Field(description="the user's words")] = None,
    ) -> dict[str, Any]:
        values = locals()
        values["kb_tags"] = kb_tags or []
        return await run("kb_find_articles", values)

    async def kb_get_article(
        kb_ref: Annotated[str, Field(description="e.g. KB-PRN-001")],
    ) -> dict[str, Any]:
        return await run("kb_get_article", locals())

    async def kb_guide_start(
        kb_ref: Annotated[str, Field(description="the chosen article, e.g. KB-PRN-001")],
        subject_node_id: Annotated[
            str | None, Field(description="the device / element being fixed")
        ] = None,
        finding_ids: Annotated[
            list[str] | None, Field(description="finding ids this guide addresses")
        ] = None,
    ) -> dict[str, Any]:
        values = locals()
        values["finding_ids"] = finding_ids or []
        return await run("kb_guide_start", values)

    async def kb_guide_get_state(guide_id: GuideId) -> dict[str, Any]:
        return await run("kb_guide_get_state", locals())

    async def kb_guide_record_outcome(
        guide_id: GuideId,
        step_id: Annotated[str, Field(description="the CURRENT step id, e.g. S2")],
        outcome: Annotated[str, Field(description="one option key of the current step")],
        state_version: Annotated[int, Field(description="the guide's current state_version")],
        reply: Annotated[str | None, Field(description="the user's reply, as written")] = None,
    ) -> dict[str, Any]:
        return await run("kb_guide_record_outcome", locals())

    async def kb_guide_end(
        guide_id: GuideId,
        reason: Annotated[
            Literal["user_cannot_do", "abandoned"], Field(description="why the guide ends")
        ],
    ) -> dict[str, Any]:
        return await run("kb_guide_end", locals())

    async def itsm_search_incidents(
        state: Annotated[
            Literal["active", "recent_resolved", "all"], Field(description="which incidents")
        ] = "active",
        node_id: Annotated[str | None, Field(description="only incidents for this item")] = None,
        text: Annotated[str | None, Field(description="words in the title")] = None,
        limit: Annotated[int | None, Field(ge=1, le=50, description="at most")] = None,
    ) -> dict[str, Any]:
        return await run("itsm_search_incidents", locals())

    async def itsm_get_incident(
        number: Annotated[str, Field(description="incident number, INC + 8 digits")],
    ) -> dict[str, Any]:
        return await run("itsm_get_incident", locals())

    async def itsm_check_duplicates(
        text: Annotated[str, Field(description="short description of the problem")],
        finding_id: Annotated[str | None, Field(description="the finding, if any")] = None,
    ) -> dict[str, Any]:
        return await run("itsm_check_duplicates", locals())

    async def itsm_prepare_draft(
        short_description: Annotated[str, Field(description="title, at most 80 characters")],
        description: Annotated[str, Field(description="what happened, from the evidence only")],
        impact: Level,
        urgency: Level,
        u_symptom: Annotated[str | None, Field(description="the user's own words")] = None,
        subcategory: Annotated[str | None, Field(description="from the catalogue list")] = None,
        category: Annotated[
            Literal["network", "hardware", "software", "inquiry"] | None,
            Field(description="only when there is no affected device"),
        ] = None,
        reasons: Annotated[
            dict[str, str] | None, Field(description="one line each: impact, urgency")
        ] = None,
        finding_id: Annotated[str | None, Field(description="the finding being raised")] = None,
        guide_id: Annotated[str | None, Field(description="the ended guide, if any")] = None,
        acknowledge_possible_duplicates: Annotated[
            bool, Field(description="true only after the manager accepted a possible duplicate")
        ] = False,
    ) -> dict[str, Any]:
        values = locals()
        values["reasons"] = reasons or {}
        return await run("itsm_prepare_draft", values)

    async def itsm_revise_draft(
        draft_id: DraftId,
        version: Annotated[int, Field(description="the draft's current version")],
        short_description: Annotated[str | None, Field(description="new title")] = None,
        description: Annotated[str | None, Field(description="new description")] = None,
        subcategory: Annotated[str | None, Field(description="from the catalogue list")] = None,
        impact: Annotated[int | None, Field(ge=1, le=3, description="1 = high")] = None,
        urgency: Annotated[int | None, Field(ge=1, le=3, description="1 = high")] = None,
        reasons: Annotated[dict[str, str] | None, Field(description="why")] = None,
    ) -> dict[str, Any]:
        values = locals()
        values["reasons"] = reasons or {}
        return await run("itsm_revise_draft", values)

    async def itsm_get_draft(draft_id: DraftId) -> dict[str, Any]:
        return await run("itsm_get_draft", locals())

    wrappers = [
        store_get_findings, store_resolve_device, store_get_node_status,
        store_get_node_history, graph_neighbourhood, graph_impact_of, graph_explain_path,
        kb_find_articles, kb_get_article, kb_guide_start, kb_guide_get_state,
        kb_guide_record_outcome, kb_guide_end, itsm_search_incidents, itsm_get_incident,
        itsm_check_duplicates, itsm_prepare_draft, itsm_revise_draft, itsm_get_draft,
    ]  # fmt: skip
    names = {w.__name__ for w in wrappers}
    if names != set(TOOLS):
        raise RuntimeError(f"wrappers and registry differ: {sorted(names ^ set(TOOLS))}")
    for wrapper in wrappers:
        wrapper.__doc__ = TOOLS[wrapper.__name__].description
        mcp.tool(wrapper)

    if hasattr(mcp, "custom_route"):

        @mcp.custom_route("/healthz", methods=["GET"])
        async def healthz(request: Request) -> JSONResponse:
            database = "ok"
            try:
                async with runtime.factory() as session:
                    await session.execute(text("SELECT 1"))
            except Exception as exc:
                database = f"error: {exc.__class__.__name__}"
            return JSONResponse(
                {
                    "status": "ok" if database == "ok" else "degraded",
                    "database": database,
                    "tools": len(TOOLS),
                    "policy_version": runtime.policy.version_text,
                    "catalogue_version": runtime.catalogue.version,
                    "embeddings": runtime.embedder.model_key if runtime.embedder else None,
                }
            )

    return mcp


def http_app(mcp: FastMCP) -> Any:
    options: dict[str, Any] = {"path": MCP_PATH}
    if "stateless_http" in inspect.signature(mcp.http_app).parameters:
        options["stateless_http"] = True
    return mcp.http_app(**options)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class InProcessServer:
    """Run the MCP server inside this process for an async with (tests, scripts)."""

    def __init__(self, runtime: ToolRuntime, port: int | None = None) -> None:
        self.runtime = runtime
        self.port = port or free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.url = f"{self.base}{MCP_PATH}"
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> InProcessServer:
        app = http_app(build_server(self.runtime))
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return self
            await asyncio.sleep(0.1)
        raise RuntimeError("the MCP server did not start within 10 seconds")

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task


def envelope_from_result(result: Any) -> dict[str, Any]:
    """The tool envelope from a fastmcp Client call_tool result."""
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        if "ok" in structured:
            return structured
        inner = structured.get("result")
        if isinstance(inner, dict) and "ok" in inner:
            return inner
    data = getattr(result, "data", None)
    if isinstance(data, dict) and "ok" in data:
        return data
    for part in getattr(result, "content", None) or []:
        text_value = getattr(part, "text", None)
        if text_value:
            return json.loads(text_value)
    raise ValueError("the tool result has no envelope")
