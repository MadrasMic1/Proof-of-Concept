# scripts/agent_spike.py
"""Phase 1 readiness spike: the path every later agentic phase depends on.

    uv run python -m scripts.agent_spike            both checks
    uv run python -m scripts.agent_spike --no-llm   check 1 only (no LLM call)

Check 1  a throwaway FastMCP server (Streamable HTTP, stateless, on 127.0.0.1) reads a
         signed token from the Authorization header; a call with the right token gets
         the store from the token, a call with a wrong token is refused.
Check 2  a Microsoft Agent Framework agent on the worker's LLM settings (LLM_PROVIDER,
         LLM_MODEL, LLM_API_KEY, LLM_BASE_URL) calls that tool over MCP and answers
         with the store from the token. Makes ONE agent run (a few LLM requests).

The library calls are matched to the INSTALLED versions by their signatures (the
keyword names differ between releases); a mismatch stops with a clear message.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import socket
import sys
from datetime import timedelta
from importlib.metadata import version
from typing import Any

import jwt
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server.dependencies import get_http_request

from app.core.clock import SystemClock
from app.core.config import Settings, get_settings

SPIKE_SECRET = "phase1-spike-secret-not-for-production-000000"
ISSUER = "ric-gateway"
AUDIENCE = "ric-mcp"
STORE = "01234"
DEFAULT_BASE_URLS = {
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "groq": "https://api.groq.com/openai/v1",
}
DEFAULT_MODELS = {
    "nvidia": "nvidia/nemotron-3.5-lightning-30b-a3b",
    "groq": "openai/gpt-oss-20b",
}


def mint_token(store: str, secret: str = SPIKE_SECRET) -> str:
    now = SystemClock().now()
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "site_code": store,
        "agent": "store_insight",
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def build_server() -> FastMCP:
    mcp = FastMCP("ric-phase1-spike")

    @mcp.tool
    def whoami() -> str:
        """Return the store code of the signed-in user. Use it to answer which store this is."""
        header = get_http_request().headers.get("authorization", "")
        token = header.removeprefix("Bearer ").strip()
        if not token:
            raise ValueError("no Authorization header reached the MCP server")
        claims = jwt.decode(
            token, SPIKE_SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER
        )
        return f"store {claims['site_code']}"

    return mcp


def first_param(target: Any, *names: str) -> str:
    """The first of names that target's signature accepts (keyword names change by release)."""
    params = inspect.signature(target).parameters
    for name in names:
        if name in params:
            return name
    raise RuntimeError(
        f"{getattr(target, '__qualname__', target)} accepts none of {names}: "
        "the installed version differs from the one this spike was written for"
    )


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SpikeServer:
    """Runs the spike MCP server in this process for the duration of an async with."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> SpikeServer:
        mcp = build_server()
        options: dict[str, Any] = {"path": "/mcp"}
        if "stateless_http" in inspect.signature(mcp.http_app).parameters:
            options["stateless_http"] = True
        app = mcp.http_app(**options)
        config = uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning")
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(100):
            if self._server.started:
                return self
            await asyncio.sleep(0.1)
        raise RuntimeError("the spike MCP server did not start within 10 seconds")

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task


def _tool_text(result: Any) -> str:
    content = getattr(result, "content", result)
    return " ".join(getattr(part, "text", str(part)) for part in content)


async def check_mcp_token() -> tuple[bool, str]:
    """Check 1: the token travels in the header and the server decides from it."""
    async with SpikeServer() as server:
        good = StreamableHttpTransport(
            server.url, headers={"Authorization": f"Bearer {mint_token(STORE)}"}
        )
        async with Client(good) as client:
            answer = _tool_text(await client.call_tool("whoami", {}))
        forged = mint_token(STORE, secret="someone-else-secret-0000000000000000")
        bad = StreamableHttpTransport(server.url, headers={"Authorization": f"Bearer {forged}"})
        try:
            async with Client(bad) as client:
                result = await client.call_tool("whoami", {})
            refused = bool(getattr(result, "is_error", False))
        except Exception:
            refused = True
    ok = STORE in answer and refused
    return ok, f"right token -> {answer!r}; forged token refused: {refused}"


def _llm_target(settings: Settings) -> tuple[str, str, str]:
    provider = settings.llm_provider
    if provider == "fake":
        raise RuntimeError("LLM_PROVIDER is fake: set a real provider to run check 2")
    base_url = settings.llm_base_url or DEFAULT_BASE_URLS.get(provider)
    model = settings.llm_model or DEFAULT_MODELS.get(provider)
    if not base_url or not model:
        raise RuntimeError(f"LLM_PROVIDER={provider} needs LLM_BASE_URL and LLM_MODEL")
    if settings.llm_api_key is None:
        raise RuntimeError("LLM_API_KEY is empty")
    return base_url, model, settings.llm_api_key.get_secret_value()


async def check_agent(settings: Settings) -> tuple[bool, str]:
    """Check 2: an MAF agent calls the MCP tool over HTTP and uses its answer."""
    import agent_framework as af
    from agent_framework.openai import OpenAIChatCompletionClient

    base_url, model, api_key = _llm_target(settings)
    client = OpenAIChatCompletionClient(
        **{first_param(OpenAIChatCompletionClient, "model", "model_id"): model},
        api_key=api_key,
        base_url=base_url,
    )
    import httpx

    agent_class = getattr(af, "Agent", None) or af.ChatAgent
    tool_params = inspect.signature(af.MCPStreamableHTTPTool).parameters
    auth = {"Authorization": f"Bearer {mint_token(STORE)}"}
    async with (
        SpikeServer() as server,
        httpx.AsyncClient(headers=auth, timeout=settings.llm_timeout_seconds) as http_client,
    ):
        tool_options: dict[str, Any] = {"name": "ric_spike", "url": server.url}
        if "http_client" in tool_params:
            tool_options["http_client"] = http_client
            mode = "http_client"
        elif "header_provider" in tool_params:
            tool_options["header_provider"] = lambda _kwargs: dict(auth)
            mode = "header_provider"
        else:
            tool_options["headers"] = dict(auth)
            mode = "headers"
        print(f"  MCP auth header sent via: {mode}")
        if "load_prompts" in tool_params:
            tool_options["load_prompts"] = False
        async with af.MCPStreamableHTTPTool(**tool_options) as tool:
            agent = agent_class(
                **{first_param(agent_class, "client", "chat_client"): client},
                name="phase1_spike",
                instructions=(
                    "You answer which store the user is signed in to. Always call the "
                    "whoami tool once, then reply with the store code it returned."
                ),
                tools=tool,
            )
            response = await asyncio.wait_for(
                agent.run("Which store am I signed in to?"),
                timeout=settings.llm_timeout_seconds * 3,
            )
    answer = getattr(response, "text", None) or str(response)
    return STORE in answer, f"{settings.llm_provider}:{model} answered {answer.strip()[:160]!r}"


def print_versions() -> None:
    for name in ("agent-framework-core", "agent-framework-openai", "fastmcp", "mcp", "pyjwt"):
        print(f"  {name} {version(name)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-llm", action="store_true", help="run check 1 only")
    args = parser.parse_args()
    print("versions:")
    print_versions()
    failed = False
    try:
        ok, detail = await check_mcp_token()
    except Exception as exc:
        ok, detail = False, f"{exc.__class__.__name__}: {exc}"
    print(f"[{'OK' if ok else 'FAIL'}] check 1 MCP + token: {detail}")
    failed |= not ok
    if not args.no_llm:
        try:
            ok, detail = await check_agent(get_settings())
        except Exception as exc:
            ok, detail = False, f"{exc.__class__.__name__}: {exc}"
        print(f"[{'OK' if ok else 'FAIL'}] check 2 agent -> MCP tool: {detail}")
        failed |= not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
