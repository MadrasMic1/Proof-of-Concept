# app/agentic/agents/bridge.py
"""Our tool bridge: the specialist's tools as Agent Framework function tools that call
the RIC MCP server (FastMCP Client, Streamable HTTP, Bearer token in the header).

Why not MAF's MCPStreamableHTTPTool: it dropped our Authorization header (Agentic
Phase 1 spike), and we need the run's tool record (for the validators), untrusted-text
wrapping, the per-run tool-call cap, and errors returned to the model as DATA (a raised
error made the framework retry the same call). Names, descriptions and argument models
come from the Phase 5 registry, so the agent and the server share one schema; the
server still validates and authorises every call.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import agent_framework as af
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from pydantic import BaseModel

from app.agentic.agents.schemas import ToolCallTrace
from app.agentic.tools.registry import TOOLS
from mcp_server.app import envelope_from_result

UNTRUSTED_OPEN = '<untrusted source="ric-data">'
UNTRUSTED_CLOSE = "</untrusted>"


@dataclass
class ToolCallRecord:
    tool: str
    arguments: dict[str, Any]
    envelope: dict[str, Any]

    @property
    def ok(self) -> bool:
        return bool(self.envelope.get("ok"))

    def trace(self) -> ToolCallTrace:
        error = self.envelope.get("error") or {}
        return ToolCallTrace(
            tool=self.tool, arguments=self.arguments, ok=self.ok, error_code=error.get("code")
        )


def wrap_untrusted(data: Any, path: str) -> Any:
    """Wrap the text at path ("data.step.instruction", "data.items[].text")."""
    if not path:
        if isinstance(data, str):
            return f"{UNTRUSTED_OPEN}{data}{UNTRUSTED_CLOSE}"
        return data
    head, _, rest = path.partition(".")
    if head.endswith("[]"):
        key = head[:-2]
        if isinstance(data, dict) and isinstance(data.get(key), list):
            data[key] = [wrap_untrusted(item, rest) for item in data[key]]
        return data
    if isinstance(data, dict) and head in data:
        data[head] = wrap_untrusted(data[head], rest)
    return data


def for_model(envelope: dict[str, Any]) -> str:
    """The tool result text the model reads (untrusted text wrapped)."""
    shown = json.loads(json.dumps(envelope, default=str))
    for path in envelope.get("untrusted_fields") or []:
        shown = wrap_untrusted(shown, path)
    return json.dumps(shown, ensure_ascii=False, default=str)


def first_param(target: Any, *names: str) -> str:
    params = inspect.signature(target).parameters
    for name in names:
        if name in params:
            return name
    raise RuntimeError(f"{target} accepts none of {names}: agent-framework differs")


def make_tool(
    name: str,
    description: str,
    input_model: type[BaseModel],
    func: Callable[..., Awaitable[str]],
) -> Any:
    """An Agent Framework FunctionTool whose arguments are validated by input_model."""
    params = inspect.signature(af.FunctionTool.__init__).parameters
    options: dict[str, Any] = {"name": name, "description": description}
    options[first_param(af.FunctionTool.__init__, "func", "function")] = func
    if "input_model" in params:
        options["input_model"] = input_model
    elif "args_schema" in params:
        options["args_schema"] = input_model
    else:
        raise RuntimeError("FunctionTool takes no input model: the installed version differs")
    return af.FunctionTool(**options)


def _fields(model: type[BaseModel], values: dict[str, Any]) -> dict[str, Any]:
    raw = values.get("arguments") if set(values) == {"arguments"} else values
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    return {k: v for k, v in dict(raw or {}).items() if k in model.model_fields and v is not None}


@dataclass
class McpBridge:
    url: str
    token: str
    max_tool_calls: int
    records: list[ToolCallRecord] = field(default_factory=list)
    listed: set[str] = field(default_factory=set)
    _client: Client | None = None

    async def __aenter__(self) -> McpBridge:
        transport = StreamableHttpTransport(
            self.url, headers={"Authorization": f"Bearer {self.token}"}
        )
        self._client = Client(transport)
        await self._client.__aenter__()
        self.listed = {tool.name for tool in await self._client.list_tools()}
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc)

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if len(self.records) >= self.max_tool_calls:
            envelope = {
                "ok": False,
                "data": None,
                "as_of": None,
                "untrusted_fields": [],
                "error": {
                    "code": "rate_limited",
                    "message": "tool call limit for this task reached: call submit_result now",
                    "retryable": False,
                },
            }
        else:
            assert self._client is not None
            try:
                envelope = envelope_from_result(await self._client.call_tool(tool, arguments))
            except Exception as exc:
                envelope = {
                    "ok": False,
                    "data": None,
                    "as_of": None,
                    "untrusted_fields": [],
                    "error": {
                        "code": "unavailable",
                        "message": f"the tool could not be called ({exc.__class__.__name__})",
                        "retryable": True,
                    },
                }
        self.records.append(ToolCallRecord(tool, arguments, envelope))
        return envelope

    def function_tools(self, allowed: list[str]) -> list[Any]:
        missing = sorted(set(allowed) - self.listed)
        if missing:
            raise RuntimeError(f"the MCP server does not list {missing}")
        tools = []
        for name in allowed:
            spec = TOOLS[name]

            def bind(tool_name: str, model: type[BaseModel]) -> Callable[..., Awaitable[str]]:
                async def run(**values: Any) -> str:
                    return for_model(await self.call(tool_name, _fields(model, values)))

                run.__name__ = tool_name
                return run

            func = bind(name, spec.args_model)
            tools.append(make_tool(name, spec.description, spec.args_model, func))
        return tools


def submit_tool(report_model: type[BaseModel], capture: Callable[[dict[str, Any]], None]) -> Any:
    async def submit_result(**values: Any) -> str:
        capture(_fields(report_model, values))
        return "Result received. Do not call any more tools; reply DONE."

    return make_tool(
        "submit_result",
        "Call exactly once, at the end, with your final result. Use only ids that appeared "
        "in this task's tool results.",
        report_model,
        submit_result,
    )
