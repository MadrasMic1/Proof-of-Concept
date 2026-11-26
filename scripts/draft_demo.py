# scripts/draft_demo.py
"""Prepare an incident draft from the terminal, as Incident Desk will (no LLM).

    uv run python -m scripts.draft_demo --site 03340 --root "RF Gun 02" --impact 1 --urgency 1
    uv run python -m scripts.draft_demo --site 01234 --root WAN01234-PRI
    uv run python -m scripts.draft_demo --site 02112 --category inquiry
        --subject "Music system silent" --description "The in-store music has stopped."

Creates a throw-away chat session for user "draft.demo" (store_manager), runs the
duplicate check, the baseline and the validation, prints the result and ALWAYS rolls
back: no draft, chat or incident is kept.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import insert

from app.agentic.config import get_agentic_settings
from app.agentic.drafts.service import (
    DraftContext,
    DraftError,
    DraftProposal,
    fresh_snapshot,
    prepare_draft,
)
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_graph import load_configured_catalogue
from app.agentic.values import AgentRole
from app.core.clock import SystemClock
from app.db.session import create_engine_from_settings, create_session_factory
from app.models import AgentChatSession

DEMO_USER = "draft.demo"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--root", help="name of the finding's root node, e.g. 'RF Gun 02'")
    parser.add_argument("--subject")
    parser.add_argument("--description")
    parser.add_argument("--symptom", default=None)
    parser.add_argument("--category", default=None, help="only without --root")
    parser.add_argument("--subcategory", default=None)
    parser.add_argument("--impact", type=int, default=3)
    parser.add_argument("--urgency", type=int, default=3)
    parser.add_argument("--acknowledge", action="store_true", help="accept possible duplicates")
    args = parser.parse_args()
    settings = get_agentic_settings()
    catalogue = load_configured_catalogue(settings)
    now = SystemClock().now()
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            store_id = await store_id_for_site(session, args.site)
            if store_id is None:
                print(f"[FAIL] store {args.site} not found")
                return 1
            chat_id = uuid.uuid4()
            await session.execute(
                insert(AgentChatSession).values(
                    id=chat_id,
                    store_id=store_id,
                    user_id=DEMO_USER,
                    role=AgentRole.STORE_MANAGER,
                )
            )
            ctx = DraftContext(store_id, DEMO_USER, chat_id, AgentRole.STORE_MANAGER)
            finding_id = None
            if args.root:
                snapshot = await fresh_snapshot(
                    session, store_id, now=now, catalogue=catalogue, settings=settings
                )
                finding_id = next(
                    (f.finding_id for f in snapshot.findings if f.root_node.name == args.root),
                    None,
                )
                if finding_id is None:
                    names = ", ".join(f.root_node.name for f in snapshot.findings) or "none"
                    print(f"[FAIL] no finding with root {args.root!r} (findings: {names})")
                    await session.rollback()
                    return 1
            subject = args.subject or f"{args.root or 'Store'} - reported by the store"
            default = (
                f"The store reports a problem with {args.root}."
                if args.root
                else "The store reports a problem."
            )
            description = args.description or default
            try:
                result = await prepare_draft(
                    session,
                    ctx,
                    DraftProposal(
                        short_description=subject,
                        description=description,
                        u_symptom=args.symptom,
                        category=args.category,
                        subcategory=args.subcategory,
                        impact=args.impact,
                        urgency=args.urgency,
                        finding_id=finding_id,
                        acknowledge_possible_duplicates=args.acknowledge,
                    ),
                    now=now,
                    catalogue=catalogue,
                    settings=settings,
                )
            except DraftError as exc:
                print(f"[{exc.code}] {exc.message}")
                await session.rollback()
                return 1
            print(f"outcome: {result.outcome} (duplicate check: {result.duplicates.result})")
            for match in result.duplicates.matches:
                ref = match.incident_number or match.draft_id
                print(f"  {match.level}: {ref} - {match.reason}")
            draft = result.draft
            if draft is not None:
                print(
                    f"draft {draft.draft_id} v{draft.version} {draft.status}, "
                    f"expires {draft.expires_at:%H:%M} UTC"
                )
                print(f"  {draft.short_description}")
                print(
                    f"  category {draft.category}/{draft.subcategory} -> "
                    f"{draft.assignment_group}; affected "
                    f"{draft.affected.name if draft.affected else 'none'}"
                )
                print(
                    f"  impact {draft.impact} urgency {draft.urgency} priority {draft.priority} "
                    f"(baseline {draft.baseline_impact}/{draft.baseline_urgency})"
                )
                for key, value in draft.reasons.items():
                    print(f"    {key}: {value}")
                print("  description:")
                for line in draft.description.splitlines():
                    print(f"    {line}")
            await session.rollback()
            print("rolled back (nothing kept)")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
