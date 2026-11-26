# scripts/mcp_call.py
"""Call one MCP tool exactly as an agent will: mint a token, send it in the header.

    uv run python -m scripts.mcp_call --site 03340 --agent store_insight
        --tool store_get_findings
    uv run python -m scripts.mcp_call --site 01456 --agent kb_guide --chat
        --tool kb_guide_start --arg kb_ref=KB-PRN-001

--arg name=value is repeatable; a value that is valid JSON (numbers, true, lists) is
used as JSON, anything else as text (PowerShell-friendly: no JSON quoting needed).

Starts the server in this process (as ric_mcp) unless --url points at a running one.
--chat creates a throw-away chat session (app login) and removes it, with any guide,
draft and audit rows of it, at the end. Prints the envelope.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import delete, insert

from app.agentic.common.tokens import mint_token
from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.values import AgentRole, SpecialistName
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.models import (
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbGuideSession,
    AgentToolCall,
)
from mcp_server.app import InProcessServer, envelope_from_result
from mcp_server.runtime import (
    StartupError,
    create_mcp_engine,
    create_runtime,
    load_checked_policy,
)


def parse_args(pairs: list[str]) -> dict[str, object]:
    values: dict[str, object] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        if not sep or not name.strip():
            raise ValueError(f"--arg {pair!r} must be name=value")
        try:
            values[name.strip()] = json.loads(raw)
        except ValueError:
            values[name.strip()] = raw
    return values


async def call(url: str, token: str, tool: str, args: dict[str, object]) -> dict[str, object]:
    transport = StreamableHttpTransport(url, headers={"Authorization": f"Bearer {token}"})
    async with Client(transport) as client:
        return envelope_from_result(await client.call_tool(tool, args))


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--agent", required=True, choices=[a.value for a in SpecialistName])
    parser.add_argument("--role", default="store_manager", choices=[r.value for r in AgentRole])
    parser.add_argument("--user", default="mcp.check")
    parser.add_argument("--tool", required=True)
    parser.add_argument("--arg", action="append", default=[], help="name=value (repeatable)")
    parser.add_argument("--chat", action="store_true", help="use a throw-away chat session")
    parser.add_argument("--url", default=None, help="a running server, e.g. MCP_URL")
    options = parser.parse_args()
    settings = get_agentic_settings()
    try:
        tool_args = parse_args(options.arg)
        policy = load_checked_policy(settings)
    except (ValueError, StartupError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    app_engine = create_engine_from_settings()
    factory = create_session_factory(app_engine)
    chat_id = uuid.uuid4() if options.chat else None
    try:
        async with factory() as session:
            store_id = await store_id_for_site(session, options.site)
            if store_id is None:
                print(f"[FAIL] store {options.site} not found")
                return 1
            if chat_id is not None:
                await session.execute(
                    insert(AgentChatSession).values(
                        id=chat_id, store_id=store_id, user_id=options.user, role=options.role
                    )
                )
                await session.commit()
        token = mint_token(
            settings,
            user_id=options.user,
            role=options.role,
            store_id=store_id,
            agent=options.agent,
            correlation_id=uuid.uuid4(),
            policy_version=policy.version_text,
            chat_session_id=chat_id,
        )
        if options.url:
            envelope = await call(options.url, token, options.tool, tool_args)
        else:
            mcp_engine = create_mcp_engine(settings)
            try:
                runtime = create_runtime(settings, get_settings(), mcp_engine)
                async with InProcessServer(runtime) as server:
                    envelope = await call(server.url, token, options.tool, tool_args)
            finally:
                await mcp_engine.dispose()
        print(json.dumps(envelope, indent=2, default=str))
        return 0 if envelope.get("ok") else 2
    finally:
        if chat_id is not None:
            async with factory() as session:
                for model, column in (
                    (AgentToolCall, AgentToolCall.chat_session_id),
                    (AgentIncidentDraft, AgentIncidentDraft.chat_session_id),
                    (AgentKbGuideSession, AgentKbGuideSession.chat_session_id),
                    (AgentChatSession, AgentChatSession.id),
                ):
                    await session.execute(delete(model).where(column == chat_id))
                await session.commit()
            print("throw-away chat session removed")
        await app_engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
