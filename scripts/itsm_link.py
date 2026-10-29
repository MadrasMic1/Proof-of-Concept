# scripts/itsm_link.py
"""Link RIC rows to the GLPI objects that already exist for them (Phase 15).

    uv run python -m scripts.itsm_link              link, queue first syncs, set watermark
    uv run python -m scripts.itsm_link --dry-run    report only, write nothing
    uv run python -m scripts.itsm_link --relink     also change links that point elsewhere

Fills the glpi_* id columns of stores, assignment groups, network elements,
devices, incidents and journal entries; queues one itsm_inbox row (source
'link') per incident ticket of a RIC store; sets the itsm_reconcile watermark.
Creates nothing in GLPI and copies no incident content (that is the sync).
Safe to run more than once. Run it again after "scripts.seed itsm" (the seed
recreates incidents and journal entries without their GLPI links).
Exit code 1 on a GLPI or configuration error, or when another run is busy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.itsm.errors import ItsmConfigError, ItsmError
from app.itsm.factory import build_itsm_client
from app.itsm.link import KINDS, LinkBusyError, LinkResult, run_link

MAX_DETAILS = 20


def print_result(result: LinkResult) -> None:
    inventory, plan = result.inventory, result.plan
    print(
        f"GLPI: {len(inventory.locations)} locations, {len(inventory.groups)} groups, "
        f"{len(inventory.assets)} assets, {len(inventory.tickets)} tickets"
    )
    print()
    print(
        f"{'':<10} {'linked':>7} {'already':>8} {'no match':>9} {'ambiguous':>10} {'conflict':>9}"
    )
    for kind in KINDS:
        r = plan.reports[kind]
        print(
            f"{kind:<10} {r.linked:>7} {r.already:>8} {r.no_match:>9} "
            f"{r.ambiguous:>10} {r.conflict:>9}"
        )
    print()
    print("journal: 'no match' = RIC-only entries (e.g. close notes; system entries are not")
    print(f"         counted); GLPI follow-ups without a RIC entry: {plan.glpi_only_followups}")
    print(f"RIC incidents without a GLPI ticket: {plan.ric_only_incidents or 'none'}")
    print("GLPI tickets without a RIC incident (created by the first sync):")
    print(f"  {plan.glpi_only_tickets or 'none'}")
    if result.skipped_tickets:
        print(f"[WARN] tickets not readable (follow-ups not linked): {result.skipped_tickets}")
    for kind in KINDS:
        details = plan.reports[kind].details
        for line in details[:MAX_DETAILS]:
            print(f"  [{kind}] {line}")
        if len(details) > MAX_DETAILS:
            print(f"  [{kind}] ... {len(details) - MAX_DETAILS} more")
    print()
    if result.dry_run:
        print(
            f"[DRY RUN] nothing written. Would queue {len(plan.queue)} ticket(s) "
            f"{plan.queue}; watermark {plan.watermark}"
        )
    else:
        print(f"inbox: queued {result.queued}, already pending {result.already_queued}")
        print(f"itsm_reconcile: watermark {result.watermark}, next run {result.next_run_at}")
    if plan.problems:
        print(f"[WARN] {plan.problems} ambiguous/conflicting match(es) left unchanged (see above)")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Link RIC rows to existing GLPI objects.")
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    parser.add_argument("--relink", action="store_true", help="change links that point elsewhere")
    args = parser.parse_args()
    settings = get_settings()
    try:
        client = build_itsm_client(settings)
    except ItsmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1
    engine = create_engine_from_settings(settings)
    try:
        result = await run_link(
            create_session_factory(engine),
            client,
            now=SystemClock().now(),
            interval_seconds=settings.itsm_reconcile_interval_seconds,
            api_username=settings.glpi_api_username,
            relink=args.relink,
            dry_run=args.dry_run,
        )
    except (ItsmError, LinkBusyError) as exc:
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
        await engine.dispose()
    print_result(result)
    print(f"GLPI calls: {client.calls}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
