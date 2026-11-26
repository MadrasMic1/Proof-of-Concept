# scripts/kb_guide_demo.py
"""Walk a KB guide from the terminal, exactly as KB Guide will (no LLM).

    uv run python -m scripts.kb_guide_demo --site 01456 --kb KB-PRN-001 --outcomes jam_seen,works
    uv run python -m scripts.kb_guide_demo --site 01234 --kb KB-NET-001
        --outcomes cables_fine,unclear,not_works

Creates a throw-away chat session for user "kb.demo", starts the guide, records the
outcomes one by one (unclear / user_cannot_do are allowed on every step) and prints
each step. Everything is rolled back at the end, unless --keep.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from sqlalchemy import insert, select

from app.agentic.config import get_agentic_settings
from app.agentic.kb.guide import (
    GuideContext,
    GuideError,
    GuideView,
    record_outcome,
    start_guide,
)
from app.agentic.values import AgentRole
from app.core.clock import SystemClock
from app.db.session import create_engine_from_settings, create_session_factory
from app.models import AgentChatSession, Store

DEMO_USER = "kb.demo"


def show(view: GuideView) -> None:
    print(
        f"  guide {view.kb_ref} v{view.kb_version}: {view.status} "
        f"(state_version {view.state_version})"
    )
    if view.step:
        print(f"  step {view.step.step_no} [{view.step.step_id}] {view.step.instruction}")
        print(f"    ask: {view.step.ask}")
        print("    options: " + ", ".join(f"{o.key} ({o.label})" for o in view.step.options))
    elif view.steps_tried:
        tried = "; ".join(f"{t.step_id} -> {t.outcome_label}" for t in view.steps_tried)
        print(f"  steps tried: {tried}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", required=True)
    parser.add_argument("--kb", required=True, help="kb_ref, e.g. KB-PRN-001")
    parser.add_argument("--outcomes", default="", help="comma-separated outcome keys")
    parser.add_argument("--keep", action="store_true", help="commit instead of rolling back")
    args = parser.parse_args()
    settings = get_agentic_settings()
    clock = SystemClock()
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            store_id = await session.scalar(select(Store.id).where(Store.site_code == args.site))
            if store_id is None:
                print(f"[FAIL] store {args.site} not found")
                return 1
            chat_id = uuid.uuid4()
            await session.execute(
                insert(AgentChatSession).values(
                    id=chat_id, store_id=store_id, user_id=DEMO_USER, role=AgentRole.STORE_STAFF
                )
            )
            ctx = GuideContext(store_id=store_id, user_id=DEMO_USER, chat_session_id=chat_id)
            try:
                view = await start_guide(session, ctx, kb_ref=args.kb, now=clock.now())
                print("start")
                show(view)
                for outcome in [o.strip() for o in args.outcomes.split(",") if o.strip()]:
                    if view.step is None:
                        print(f"[STOP] the guide has ended; '{outcome}' not recorded")
                        break
                    view = await record_outcome(
                        session,
                        ctx,
                        uuid.UUID(view.guide_id),
                        step_id=view.step.step_id,
                        outcome=outcome,
                        state_version=view.state_version,
                        now=clock.now(),
                        max_clarifications=settings.kb_guide_max_clarifications,
                        abandon_minutes=settings.kb_guide_abandon_minutes,
                    )
                    print(f"after '{outcome}'")
                    show(view)
            except GuideError as exc:
                print(f"[{exc.code}] {exc.message}")
                await session.rollback()
                return 1
            if args.keep:
                await session.commit()
                print(f"kept: chat session {chat_id}")
            else:
                await session.rollback()
                print("rolled back (nothing kept)")
    finally:
        await engine.dispose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
