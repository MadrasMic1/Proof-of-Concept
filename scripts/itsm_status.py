# scripts/itsm_status.py
"""Health of the GLPI sync (read-only).

    uv run python -m scripts.itsm_status            from the database only
    uv run python -m scripts.itsm_status --live     plus GLPI logins and clock drift

Prints one line per check (OK / INFO / WARN / FAIL), the failed and recent conflict
inbox rows, and the verdict. Exit code 1 when the verdict is FAIL.
The same report is served as JSON by GET /api/v1/admin/itsm/status.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.itsm.errors import ItsmConfigError
from app.itsm.factory import build_itsm_client
from app.itsm.status import FAIL, Check, StatusReport, collect_status, live_checks


def show(report: StatusReport, live: list[Check]) -> None:
    print(f"GLPI sync status at {report.checked_at.isoformat(timespec='seconds')}")
    for check in [*report.checks, *live]:
        print(f"[{check.level:<4}] {check.area:<9} {check.message}")
    if report.inbox is not None:
        statuses = ", ".join(f"{k} {v}" for k, v in sorted(report.inbox.by_status.items()))
        print(f"\ninbox rows: {statuses or 'none'}")
    if report.problems:
        print("\nproblem rows (newest first):")
        for row in report.problems:
            error = (row.last_error or "").replace("\n", " ")[:120]
            print(
                f"  {row.status:<8} inbox {row.inbox_id:<6} ticket {row.glpi_ticket_id:<6} "
                f"{row.incident_number or '-':<12} attempts {row.attempts}  {error}"
            )
    if report.links is not None and report.links.examples:
        print(f"\nnot linked to GLPI (examples): {', '.join(report.links.examples)}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Show the health of the GLPI sync.")
    parser.add_argument("--live", action="store_true", help="also check GLPI logins and clock")
    args = parser.parse_args()
    settings = get_settings()
    engine = create_engine_from_settings(settings)
    try:
        async with create_session_factory(engine)() as session:
            report = await collect_status(session, now=SystemClock().now(), settings=settings)
    finally:
        await engine.dispose()
    live: list[Check] = []
    if args.live:
        try:
            client = build_itsm_client(settings)
        except ItsmConfigError as exc:
            live.append(Check("glpi", FAIL, f"configuration: {exc}"))
        else:
            try:
                live = await live_checks(client, settings)
            finally:
                await client.aclose()
    show(report, live)
    verdict = FAIL if any(check.level == FAIL for check in live) else report.verdict
    print(f"\nverdict: {verdict}")
    return 1 if verdict == FAIL else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
