# scripts/chat_cli.py
"""Chat with the RIC Store Assistant in the terminal (real model, your data).

    uv run python -m scripts.chat_cli --site 03340 --user ravi --role store_staff
    uv run python -m scripts.chat_cli --site 01234 --user priya --role store_manager
        --say "why are card payments slow" --say "is there a ticket for it"
    (the second example is one command line)

Interactive: type a message; /new starts a new conversation, /state shows the state,
/quit ends. --say runs those messages in order and exits (no prompt).
Starts the MCP server in this process (as ric_mcp) unless --mcp-url is given.
Conversations are KEPT (this is how the app will store them).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from app.agentic.chat.schemas import AssistantResponse, TurnRequest
from app.agentic.chat.sessions import close_session, ensure_session
from app.agentic.chat.turn import ChatDeps, build_chat_deps, compact_chat, run_turn
from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph import load_configured_catalogue
from app.agentic.tools.policy import ToolPolicy
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.llm.types import LlmConfigError
from app.models import AgentChatSession
from mcp_server.app import InProcessServer
from mcp_server.runtime import (
    StartupError,
    create_mcp_engine,
    create_runtime,
    load_checked_policy,
)


def show(response: AssistantResponse, seconds: float) -> None:
    print(f"\nRIC> {response.answer}")
    for card in response.cards:
        print(f"  [{card.kind}] {card.title}")
        if card.kind == "kb_step":
            print(f"      step {card.data['step_no']}: {card.data['instruction']}")
            print(f"      {card.data['ask']}")
        elif card.kind == "finding":
            for cause in card.data["causes"][:3]:
                covered = cause.get("covered_by_incident")
                print(
                    f"      {cause['root_node']['label']}: {cause['status']}"
                    + (f" - already reported as {covered}" if covered else "")
                )
        elif card.kind == "incident_draft":
            d = card.data
            print(
                f"      priority {d.get('priority')} -> {d.get('assignment_group')}; "
                f"expires {d.get('expires_at')}"
            )
        elif card.kind in ("incident_list", "duplicate_notice"):
            items = card.data.get("incidents") or card.data.get("matches") or []
            for item in items[:5]:
                number = item.get("number") or item.get("incident_number")
                print(f"      {number} {item.get('short_description', '')}")
    if response.pending_action:
        print(f"  [ACTION] Confirm draft: {response.pending_action.short_description}")
    if response.suggested_replies:
        print("  replies: " + " | ".join(response.suggested_replies))
    note = f"state {response.state} | {seconds:.1f}s"
    if response.warnings:
        note += " | warnings: " + "; ".join(response.warnings)
    print(f"  ({note})")


async def one_turn(
    deps: ChatDeps, store_id: uuid.UUID, args: argparse.Namespace, text: str
) -> None:
    loop = asyncio.get_running_loop()
    started = loop.time()
    response = await run_turn(
        deps,
        TurnRequest(
            store_id=store_id,
            user_id=args.user,
            role=args.role,
            text=text,
            client_message_id=uuid.uuid4(),
        ),
    )
    show(response, loop.time() - started)
    await compact_chat(deps, uuid.UUID(response.session_id))


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--site", required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--role", default="store_staff", choices=["store_staff", "store_manager"])
    p.add_argument("--say", action="append", default=[], help="message to send (repeatable)")
    p.add_argument("--new", action="store_true", help="start a new conversation")
    p.add_argument("--mcp-url", help="a running MCP server, e.g. MCP_URL")
    args = p.parse_args()
    settings = get_agentic_settings()
    core = get_settings()
    catalogue = load_configured_catalogue(settings)
    engine = create_engine_from_settings()
    factory = create_session_factory(engine)
    async with factory() as session:
        store_id = await store_id_for_site(session, args.site)
    if store_id is None:
        print(f"[FAIL] store {args.site} not found")
        await engine.dispose()
        return 1

    async def chat(mcp_url: str, policy: ToolPolicy) -> None:
        deps = build_chat_deps(settings, core, engine, catalogue, policy, mcp_url)
        if args.new:
            sid = await ensure_session(
                factory, store_id=store_id, user_id=args.user, role=args.role,
                now=deps.clock.now(), idle_minutes=settings.agent_session_idle_minutes,
            )  # fmt: skip
            await close_session(factory, sid, deps.clock.now())
        print(f"RIC Store Assistant - store {args.site}, {args.user} ({args.role})")
        if args.say:
            for text in args.say:
                print(f"\nyou> {text}")
                await one_turn(deps, store_id, args, text)
            return
        print("Type a message. /new = new conversation, /state, /quit")
        while True:
            text = (await asyncio.to_thread(input, "\nyou> ")).strip()
            if not text:
                continue
            if text == "/quit":
                return
            if text == "/new":
                sid = await ensure_session(
                    factory, store_id=store_id, user_id=args.user, role=args.role,
                    now=deps.clock.now(), idle_minutes=settings.agent_session_idle_minutes,
                )  # fmt: skip
                await close_session(factory, sid, deps.clock.now())
                print("new conversation")
                continue
            if text == "/state":
                async with factory() as session:
                    sid = await ensure_session(
                        factory, store_id=store_id, user_id=args.user, role=args.role,
                        now=deps.clock.now(), idle_minutes=settings.agent_session_idle_minutes,
                    )  # fmt: skip
                    row = await session.get(AgentChatSession, sid)
                    print(json.dumps({"state": row.state, "turns": row.turn_count,
                                      "data": row.state_data}, indent=2, default=str))  # fmt: skip
                continue
            await one_turn(deps, store_id, args, text)

    try:
        if args.mcp_url:
            await chat(args.mcp_url, load_checked_policy(settings))
        else:
            mcp_engine = create_mcp_engine(settings)
            try:
                runtime = create_runtime(settings, core, mcp_engine)
                async with InProcessServer(runtime) as server:
                    await chat(server.url, runtime.policy)
            finally:
                await mcp_engine.dispose()
    except (StartupError, LlmConfigError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
