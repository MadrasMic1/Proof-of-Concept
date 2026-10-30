# scripts/itsm_resync.py
"""Recovery commands for the GLPI sync. They only QUEUE work: the worker syncs.

    uv run python -m scripts.itsm_resync --ticket 3                one GLPI ticket
    uv run python -m scripts.itsm_resync --number INC00001234      one RIC incident
    uv run python -m scripts.itsm_resync --all                     every linked incident
    uv run python -m scripts.itsm_resync --all --quiet             same, imported history skipped
    uv run python -m scripts.itsm_resync --retry-failed            failed inbox rows again
    uv run python -m scripts.itsm_resync --list-conflicts          conflicts of the last 7 days
    uv run python -m scripts.itsm_resync --start-watermark         reconciliation starts now
    uv run python -m scripts.itsm_resync --start-watermark --force replace an existing start

--quiet: follow-ups found by these syncs are imported 'skipped' (never sent to the LLM).
--start-watermark: the 15-minute check needs a starting point; scripts.itsm_link sets it
from the newest GLPI ticket. With no ticket in GLPI yet, this sets it to GLPI's current
time (read from GLPI's HTTP Date header). Safe to run more than once.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory, session_scope
from app.itsm.resync import (
    conflict_rows,
    linked_ticket_ids,
    queue_tickets,
    retry_failed,
    start_watermark,
    ticket_of,
)
from app.itsm.status import glpi_server_time


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Recovery commands for the GLPI sync.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--ticket", type=int, help="GLPI ticket id")
    mode.add_argument("--number", help="RIC incident number, e.g. INC00001234")
    mode.add_argument("--all", action="store_true", help="every incident linked to GLPI")
    mode.add_argument("--retry-failed", action="store_true", help="failed inbox rows again")
    mode.add_argument("--list-conflicts", action="store_true", help="conflicts, last 7 days")
    mode.add_argument("--start-watermark", action="store_true", help="reconcile from now on")
    parser.add_argument("--quiet", action="store_true", help="imported history is skipped")
    parser.add_argument("--force", action="store_true", help="--start-watermark: replace it")
    return parser.parse_args()


async def main() -> int:
    args = parse_args()
    settings = get_settings()
    clock = SystemClock()
    engine = create_engine_from_settings(settings)
    factory = create_session_factory(engine)
    try:
        if args.list_conflicts:
            async with factory() as session:
                rows = await conflict_rows(session, now=clock.now())
            for row in rows:
                when = row.processed_at.isoformat(timespec="seconds") if row.processed_at else "-"
                print(
                    f"inbox {row.inbox_id:<6} ticket {row.glpi_ticket_id:<6} "
                    f"{row.incident_number or '-':<12} {when}  {row.reason or ''}"
                )
            print(f"{len(rows)} conflict(s) in the last 7 days")
            return 0
        if args.start_watermark:
            if not settings.glpi_v1_url:
                print("[FAIL] GLPI_V1_URL is not set in .env", file=sys.stderr)
                return 1
            try:
                glpi_time, drift = await glpi_server_time(
                    settings.glpi_v1_url, timeout=settings.glpi_timeout_seconds
                )
            except Exception as exc:
                reason = f"{exc.__class__.__name__}: {exc}"
                print(f"[FAIL] GLPI not reachable: {reason}", file=sys.stderr)
                return 1
            async with session_scope(factory) as session:
                old, new = await start_watermark(
                    session,
                    glpi_time,
                    interval_seconds=settings.itsm_reconcile_interval_seconds,
                    now=clock.now(),
                    force=args.force,
                )
            if old is not None and not args.force:
                print(f"[OK] watermark kept: {old.isoformat()} (use --force to replace it)")
            else:
                print(f"[OK] watermark set to GLPI time {new.isoformat()} (drift {drift:+.1f} s)")
            return 0
        if args.retry_failed:
            async with session_scope(factory) as session:
                requeued, superseded = await retry_failed(session, now=clock.now())
            print(
                f"[OK] failed rows requeued: {requeued}; superseded by a pending row: "
                f"{superseded}. The worker syncs them."
            )
            return 0
        async with session_scope(factory) as session:
            if args.all:
                tickets = await linked_ticket_ids(session)
            elif args.number:
                ticket = await ticket_of(session, args.number)
                if ticket is None:
                    print(
                        f"[FAIL] {args.number} is unknown or not linked to GLPI "
                        "(run scripts.itsm_link)",
                        file=sys.stderr,
                    )
                    return 1
                tickets = [ticket]
            else:
                tickets = [args.ticket]
            queued, already = await queue_tickets(
                session, tickets, now=clock.now(), quiet=args.quiet
            )
        mode = " (quiet)" if args.quiet else ""
        print(f"[OK] queued {queued}{mode}, already pending {already}. The worker syncs them.")
        return 0
    finally:
        await engine.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
