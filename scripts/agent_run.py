# scripts/agent_run.py
"""Run one specialist agent from the terminal, on the configured model (NVIDIA NIM).

    --site 01234 --agent store_insight --question "why are card payments slow"
    --site 01456 --agent kb_guide --mode start --element-type printer --fault-code PAPER_JAM
        --keep
    --site 01456 --agent kb_guide --mode continue --chat-id <id> --guide-id <id>
        --user-report "yes there was paper stuck"
    --site 01234 --agent incident_desk --mode lookup --question "any ticket for the internet"
(each after: uv run python -m scripts.agent_run)

Starts the MCP server in this process (as ric_mcp) and talks to the model over the
network. kb_guide / incident_desk need a chat session: one is created for user
"agent.run" (role --role) and removed at the end with everything it produced, unless
--keep (then reuse it with --chat-id). Prints the tool calls, tokens, time and result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

from sqlalchemy import delete, insert

from app.agentic.agents.deps import build_agent_deps
from app.agentic.agents.runtime import run_specialist
from app.agentic.agents.schemas import (
    IncidentDeskBrief,
    KbGuideBrief,
    StoreInsightBrief,
    TrustedContext,
)
from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.llm.types import LlmConfigError
from app.models import (
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbGuideSession,
    AgentLlmCall,
    AgentToolCall,
)
from mcp_server.app import InProcessServer
from mcp_server.runtime import StartupError, create_mcp_engine, create_runtime

USER = "agent.run"


def brief_from(
    args: argparse.Namespace,
) -> StoreInsightBrief | KbGuideBrief | IncidentDeskBrief:
    tags = [t.strip() for t in (args.kb_tags or "").split(",") if t.strip()]
    if args.agent == "store_insight":
        devices = [args.devices] if args.devices else []
        question = args.question or "What is wrong?"
        return StoreInsightBrief(question=question, devices_mentioned=devices)
    if args.agent == "kb_guide":
        return KbGuideBrief(
            mode=args.mode or "start",
            issue=args.question,
            element_type=args.element_type,
            fault_code=args.fault_code,
            kb_tags=tags,
            subject_node_id=args.subject_node,
            finding_ids=[args.finding_id] if args.finding_id else [],
            user_report=args.user_report,
        )
    return IncidentDeskBrief(
        mode=args.mode or "lookup",
        issue=args.question or "store issue",
        user_words=args.user_report,
        finding_id=args.finding_id,
        revision_request=args.revision,
        acknowledge_possible_duplicates=args.acknowledge,
    )


async def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--site", required=True)
    agents = ["store_insight", "kb_guide", "incident_desk"]
    p.add_argument("--agent", required=True, choices=agents)
    p.add_argument("--role", default="store_manager", choices=["store_staff", "store_manager"])
    p.add_argument("--question")
    p.add_argument("--devices")
    p.add_argument("--mode")
    p.add_argument("--element-type")
    p.add_argument("--fault-code")
    p.add_argument("--kb-tags")
    p.add_argument("--subject-node")
    p.add_argument("--finding-id")
    p.add_argument("--user-report")
    p.add_argument("--revision")
    p.add_argument("--acknowledge", action="store_true")
    p.add_argument("--chat-id")
    p.add_argument("--guide-id")
    p.add_argument("--draft-id")
    p.add_argument("--keep", action="store_true", help="keep the chat session and its rows")
    args = p.parse_args()
    settings = get_agentic_settings()
    core = get_settings()
    app_engine = create_engine_from_settings()
    factory = create_session_factory(app_engine)
    created_chat: uuid.UUID | None = None
    try:
        async with factory() as session:
            store_id = await store_id_for_site(session, args.site)
            if store_id is None:
                print(f"[FAIL] store {args.site} not found")
                return 1
            chat_id = uuid.UUID(args.chat_id) if args.chat_id else None
            if chat_id is None and args.agent != "store_insight":
                chat_id = created_chat = uuid.uuid4()
                await session.execute(
                    insert(AgentChatSession).values(
                        id=chat_id, store_id=store_id, user_id=USER, role=args.role
                    )
                )
                await session.commit()
        mcp_engine = create_mcp_engine(settings)
        try:
            runtime = create_runtime(settings, core, mcp_engine)
            async with InProcessServer(runtime) as server:
                deps = build_agent_deps(
                    settings, core, runtime.policy, server.url, usage_factory=factory
                )
                trusted = TrustedContext(
                    user_id=USER,
                    role=args.role,
                    store_id=store_id,
                    correlation_id=uuid.uuid4(),
                    chat_session_id=chat_id,
                    guide_id=uuid.UUID(args.guide_id) if args.guide_id else None,
                    draft_id=uuid.UUID(args.draft_id) if args.draft_id else None,
                )
                result = await run_specialist(args.agent, brief_from(args), trusted, deps)
        finally:
            await mcp_engine.dispose()
    except (StartupError, LlmConfigError) as exc:
        print(f"[FAIL] {exc}")
        return 1
    finally:
        if created_chat is not None and not args.keep:
            async with factory() as session:
                for model, column in (
                    (AgentToolCall, AgentToolCall.chat_session_id),
                    (AgentLlmCall, AgentLlmCall.chat_session_id),
                    (AgentIncidentDraft, AgentIncidentDraft.chat_session_id),
                    (AgentKbGuideSession, AgentKbGuideSession.chat_session_id),
                    (AgentChatSession, AgentChatSession.id),
                ):
                    await session.execute(delete(model).where(column == created_chat))
                await session.commit()
            print("throw-away chat session removed")
        elif created_chat is not None:
            print(f"kept chat session: --chat-id {created_chat}")
        await app_engine.dispose()
    t = result.trace
    stop = f" | stop: {t.stop_reason}" if t.stop_reason else ""
    print(
        f"model {t.model} | attempts {t.attempts} | model calls {t.model_calls} | "
        f"tokens {t.prompt_tokens}/{t.completion_tokens} | {t.duration_ms} ms{stop}"
    )
    for call in t.tool_calls:
        state = "ok" if call.ok else f"ERROR {call.error_code}"
        print(f"  tool {call.tool}({json.dumps(call.arguments, default=str)[:120]}) -> {state}")
    print(f"status {result.status} | outcome {result.outcome} | next {result.suggested_next}")
    print(f"summary: {result.summary}")
    for problem in result.problems:
        print(f"  problem: {problem}")
    print(json.dumps(result.body, indent=2, default=str)[:4000])
    return 0 if result.status == "ok" else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
