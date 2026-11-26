# scripts/mcp_server.py
"""Run the RIC MCP server (the agents' tools) on MCP_HOST:MCP_PORT, path /mcp.

    uv run python -m scripts.mcp_server             runs until Ctrl+C
    uv run python -m scripts.mcp_server --check     build everything, print, exit

Logs in to PostgreSQL as ric_mcp (MCP_DATABASE_URL). Health: GET /healthz.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import uvicorn

from app.agentic.config import get_agentic_settings
from app.agentic.tools.registry import TOOLS
from app.core.config import get_settings
from app.core.logging import configure_logging
from mcp_server.app import MCP_PATH, build_server, http_app
from mcp_server.runtime import StartupError, create_mcp_engine, create_runtime


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="build and exit")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = get_agentic_settings()
    core = get_settings()
    configure_logging(core.log_level, core.log_format)
    try:
        engine = create_mcp_engine(settings)
        runtime = create_runtime(settings, core, engine)
    except StartupError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    port = args.port or settings.mcp_port
    try:
        app = http_app(build_server(runtime))
        print(
            f"RIC MCP server: {len(TOOLS)} tools, policy v{runtime.policy.version_text}, "
            f"catalogue v{runtime.catalogue.version}, embeddings "
            f"{runtime.embedder.model_key if runtime.embedder else 'off'}"
        )
        print(f"listening on http://{settings.mcp_host}:{port}{MCP_PATH}")
        if args.check:
            return 0
        config = uvicorn.Config(app, host=settings.mcp_host, port=port, log_level="info")
        await uvicorn.Server(config).serve()
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
