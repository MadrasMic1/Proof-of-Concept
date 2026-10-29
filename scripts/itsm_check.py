# scripts/itsm_check.py
"""Read one GLPI ticket through the ITSM client and show it as RIC values (read-only).

    uv run python -m scripts.itsm_check                        INC00001234
    uv run python -m scripts.itsm_check --number INC00001303
    uv run python -m scripts.itsm_check --ticket 3             by GLPI ticket id
    uv run python -m scripts.itsm_check --since-hours 48       look-back of the change search

Logs in to GLPI (API V2 and V1), reads the ticket snapshot, maps it to RIC
values, compares them with the incident row in the database (if the number
exists there) and runs one changed-since search. Writes nothing to GLPI or
the database. Exit code 1 on any GLPI error.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.itsm.errors import ItsmConfigError, ItsmError
from app.itsm.factory import build_itsm_client
from app.itsm.mapping import IncidentFields, map_incident, map_journal
from app.models import AssignmentGroup, Incident, IncidentJournal

COMPARED = (
    "short_description",
    "state",
    "hold_reason",
    "impact",
    "urgency",
    "category",
    "subcategory",
    "contact_type",
    "assigned_to",
    "opened_at",
    "u_resolution_eta",
    "u_eta_note",
    "u_symptom",
)


async def database_view(number: str) -> tuple[dict[str, Any], dict[str, int]] | None:
    """The incident row (and journal counts by element) as stored in RIC, or None."""
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            row = (
                await session.execute(
                    select(Incident, AssignmentGroup.name)
                    .outerjoin(AssignmentGroup, AssignmentGroup.id == Incident.assignment_group_id)
                    .where(Incident.number == number)
                )
            ).one_or_none()
            if row is None:
                return None
            incident, group_name = row
            counts = dict(
                (
                    await session.execute(
                        select(IncidentJournal.element, func.count())
                        .where(IncidentJournal.incident_id == incident.id)
                        .group_by(IncidentJournal.element)
                    )
                ).all()
            )
            values = {name: getattr(incident, name) for name in COMPARED}
            values["group_name"] = group_name
            return values, counts
    finally:
        await engine.dispose()


def show(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else repr(value)


def show_fields(fields: IncidentFields) -> None:
    print("\n== GLPI ticket as RIC values")
    for name in (
        "glpi_ticket_id",
        "number",
        "glpi_location_id",
        "location_name",
        *COMPARED,
        "business_service",
        "caller",
        "opened_by",
        "glpi_group_id",
        "group_name",
        "ci",
        "resolved_at",
        "closed_at",
        "close_code",
        "itsm_updated_at",
        "is_incident",
        "is_deleted",
    ):
        print(f"  {name:<18} {show(getattr(fields, name))}")
    for warning in fields.warnings:
        print(f"  [WARN] {warning}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Read one GLPI ticket as RIC values.")
    parser.add_argument("--number", default="INC00001234", help="RIC incident number")
    parser.add_argument("--ticket", type=int, help="GLPI ticket id (instead of --number)")
    parser.add_argument("--since-hours", type=float, default=24.0, help="change search look-back")
    args = parser.parse_args()
    settings = get_settings()
    try:
        client = build_itsm_client(settings)
    except ItsmConfigError as exc:
        print(f"[FAIL] configuration: {exc}", file=sys.stderr)
        return 1
    print(f"provider: {settings.itsm_provider}")
    print(f"V2: {settings.glpi_v2_url}   V1: {settings.glpi_v1_url}")
    print(f"user: {settings.glpi_api_username}   timezone: {settings.glpi_timezone}")
    try:
        await client.check_connection()
        print("[OK] API V2 token and API V1 session")
        ticket_id = args.ticket
        if ticket_id is None:
            ticket_id = await client.find_ticket_id(args.number)
            if ticket_id is None:
                print(f"[FAIL] no GLPI ticket has external id {args.number}", file=sys.stderr)
                return 1
        print(f"[OK] GLPI ticket {ticket_id}")
        snapshot = await client.get_ticket_snapshot(ticket_id)
        fields = map_incident(snapshot)
        journal = map_journal(snapshot, settings.glpi_api_username)
        show_fields(fields)
        print(f"\n== journal from GLPI follow-ups ({len(journal)})")
        for entry in journal:
            print(
                f"  {entry.glpi_followup_id:>5} {entry.element:<10} {entry.author_type:<10} "
                f"{entry.created_by:<14} own={entry.own_write!s:<5} {entry.value[:60]!r}"
            )
        if snapshot.status == 4:
            print(f"\n== pending reason name used (V1): {snapshot.pending_reason_name!r}")
            try:
                raw = await client.get_v2_json(f"Assistance/Ticket/{ticket_id}/PendingReason")
                first = raw[0] if isinstance(raw, list) and raw else raw
                keys = sorted(first) if isinstance(first, dict) else first
                print(f"   V2 PendingReason answer (keys only, for comparison): {keys}")
            except ItsmError as exc:
                print(f"   V2 PendingReason not readable ({exc}); V1 is used")
        since = SystemClock().now() - timedelta(hours=args.since_hours)
        changed = await client.changed_since(since)
        print(f"\n== tickets changed in the last {args.since_hours:g} h: {len(changed)}")
        for item in changed[-5:]:
            print(f"  ticket {item.id}  date_mod {item.date_mod.isoformat()}")
    except ItsmError as exc:
        print(f"[FAIL] {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        await client.aclose()
        print(f"\nGLPI calls: {client.calls}")

    number = fields.number or args.number
    try:
        stored = await database_view(number)
    except Exception as exc:  # noqa: BLE001 - diagnostics only
        print(f"[WARN] database not readable: {exc.__class__.__name__}: {exc}")
        return 0
    if stored is None:
        print(f"\n[INFO] {number} is not in the RIC database")
        return 0
    values, counts = stored
    print(f"\n== RIC database row {number} vs GLPI")
    for name in (*COMPARED, "group_name"):
        mark = "same" if values[name] == getattr(fields, name) else "DIFF"
        print(
            f"  {mark:<4} {name:<18} RIC {show(values[name])}  GLPI {show(getattr(fields, name))}"
        )
    glpi_counts = {e: sum(1 for j in journal if j.element == e) for e in ("work_notes", "comments")}
    print(f"  journal RIC {counts}   GLPI follow-ups {glpi_counts}")
    print("  (DIFF lines are expected where the seed and GLPI differ, e.g. times; the sync")
    print("   in Phase 16 makes RIC follow GLPI.)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
