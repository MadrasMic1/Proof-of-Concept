# scripts/itsm_sync.py
"""Sync GLPI tickets into RIC by hand (the worker does it automatically from Phase 18).

    uv run python -m scripts.itsm_sync --ticket 3              one GLPI ticket
    uv run python -m scripts.itsm_sync --number INC00001234    one incident
    uv run python -m scripts.itsm_sync --pending               process the itsm_inbox queue
    uv run python -m scripts.itsm_sync --pending --limit 5

--pending takes due inbox rows one by one: done / ignored / conflict, or failed on
an error. If GLPI is unavailable the row goes back to pending (retried later) and
the run stops. Rows queued by scripts.itsm_link (source 'link') sync quietly: the
journal entries they import are skipped, so old history is never sent to the LLM.
Exit code 1 on a configuration error, GLPI unavailable, or any failed row.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory, session_scope
from app.itsm import inbox
from app.itsm.errors import ItsmConfigError, ItsmUnavailable
from app.itsm.factory import build_itsm_client
from app.itsm.sync import Outcome, SyncResult, sync_ticket
from app.models import Incident

RETRY_AFTER = timedelta(minutes=1)


def show(result: SyncResult) -> None:
    print(f"[{result.outcome.upper():<8}] {result.summary()}")
    for warning in result.warnings:
        print(f"           [WARN] {warning}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Sync GLPI tickets into RIC.")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--ticket", type=int, help="GLPI ticket id")
    target.add_argument("--number", help="RIC incident number, e.g. INC00001234")
    target.add_argument("--pending", action="store_true", help="process the itsm_inbox queue")
    parser.add_argument("--limit", type=int, default=0, help="--pending: at most N rows (0 = all)")
    args = parser.parse_args()
    settings = get_settings()
    clock = SystemClock()
    try:
        client = build_itsm_client(settings)
    except ItsmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    failed = 0
    try:
        if not args.pending:
            ticket_id = args.ticket
            if args.number:
                async with factory() as session:
                    ticket_id = await session.scalar(
                        select(Incident.glpi_ticket_id).where(Incident.number == args.number)
                    )
                ticket_id = ticket_id or await client.find_ticket_id(args.number)
                if ticket_id is None:
                    print(f"[FAIL] no GLPI ticket for {args.number}", file=sys.stderr)
                    return 1
            result = await sync_ticket(
                factory, client, ticket_id, now=clock.now(), api_username=settings.glpi_api_username
            )
            show(result)
            return 0
        async with session_scope(factory) as session:
            reset = await inbox.reset_processing(session, clock.now())
        if reset:
            print(f"{reset} row(s) left 'processing' by a stopped run were released")
        processed = 0
        while not args.limit or processed < args.limit:
            async with session_scope(factory) as session:
                row = await inbox.claim_next(session, clock.now())
            if row is None:
                break
            processed += 1
            try:
                result = await sync_ticket(
                    factory,
                    client,
                    row.glpi_ticket_id,
                    now=clock.now(),
                    api_username=settings.glpi_api_username,
                    quiet=row.quiet,
                )
            except ItsmUnavailable as exc:
                now = clock.now()
                async with session_scope(factory) as session:
                    await inbox.release(session, row.id, str(exc), now + RETRY_AFTER, now)
                print(f"[RETRY   ] ticket {row.glpi_ticket_id}: GLPI unavailable ({exc}); stopped")
                return 1
            except Exception as exc:  # noqa: BLE001 - one bad ticket never stops the run
                failed += 1
                async with session_scope(factory) as session:
                    await inbox.fail(
                        session, row.id, f"{exc.__class__.__name__}: {exc}", clock.now()
                    )
                print(f"[FAILED  ] ticket {row.glpi_ticket_id}: {exc.__class__.__name__}: {exc}")
                continue
            note = "; ".join(result.warnings) if result.outcome == Outcome.CONFLICT else None
            async with session_scope(factory) as session:
                await inbox.finish(session, row.id, result.outcome, clock.now(), note)
            show(result)
        print(f"\ninbox rows processed: {processed}, failed: {failed}")
    finally:
        await client.aclose()
        await engine.dispose()
        print(f"GLPI calls: {client.calls}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
