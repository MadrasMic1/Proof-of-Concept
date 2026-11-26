# scripts/store_snapshot.py
"""Show what the assistant will know about a store (Agentic Phase 2 check tool).

    uv run python -m scripts.store_snapshot --site 01234           findings + snapshot text
    uv run python -m scripts.store_snapshot --site 01234 --json    the full snapshot as JSON
    uv run python -m scripts.store_snapshot --site 01234 --find "till 2 printer"
    uv run python -m scripts.store_snapshot --validate             catalogue vs database

Read-only: one REPEATABLE READ transaction per snapshot, nothing is written.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.config import get_agentic_settings
from app.agentic.store_context.queries import store_id_for_site
from app.agentic.store_context.render import estimate_tokens, render_snapshot
from app.agentic.store_context.resolve import resolve_device
from app.agentic.store_context.snapshot import build_snapshot
from app.agentic.store_graph import Catalogue, catalogue_path, load_configured_catalogue
from app.core.clock import SystemClock
from app.db.session import create_engine_from_settings, create_session_factory
from app.models import AssignmentGroup, DeviceType, NetworkElementType


async def validate(factory: async_sessionmaker[AsyncSession], catalogue: Catalogue) -> int:
    async with factory() as session:
        device_types = (await session.scalars(select(DeviceType.code))).all()
        element_types = (await session.scalars(select(NetworkElementType.code))).all()
        groups = (await session.scalars(select(AssignmentGroup.name))).all()
    problems = catalogue.problems_against(
        device_types=device_types, element_types=element_types, assignment_groups=groups
    )
    for problem in problems:
        print(f"[FAIL] {problem}")
    status = "OK" if not problems else "PROBLEMS"
    print(
        f"catalogue v{catalogue.version}: {len(catalogue.types)} types, "
        f"{len(catalogue.services)} services - {status}"
    )
    return 1 if problems else 0


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="store site code, e.g. 01234")
    parser.add_argument("--json", action="store_true", help="print the snapshot as JSON")
    parser.add_argument("--find", help="resolve these words to devices")
    parser.add_argument("--validate", action="store_true", help="check the catalogue")
    args = parser.parse_args()
    settings = get_agentic_settings()
    catalogue = load_configured_catalogue(settings)
    engine = create_engine_from_settings()
    factory = create_session_factory(engine)
    try:
        if args.validate:
            print(f"catalogue: {catalogue_path(settings)}")
            return await validate(factory, catalogue)
        if not args.site:
            parser.error("--site is required (or use --validate)")
        async with factory() as session:
            store_id = await store_id_for_site(session, args.site)
        if store_id is None:
            print(f"[FAIL] store {args.site} not found", file=sys.stderr)
            return 1
        snapshot = await build_snapshot(
            factory, store_id, now=SystemClock().now(), catalogue=catalogue, settings=settings
        )
    finally:
        await engine.dispose()
    if args.json:
        print(snapshot.model_dump_json(indent=2))
        return 0
    if args.find:
        candidates = resolve_device(
            args.find, snapshot.nodes, catalogue, limit=settings.store_insight_max_device_options
        )
        for c in candidates:
            print(f"{c.confidence:.2f} {c.matched_on:8} {c.node.name} - {c.node.label}")
        if not candidates:
            print("no matching device")
        return 0
    text = render_snapshot(
        snapshot,
        max_tokens=settings.store_ctx_snapshot_max_tokens,
        max_device_lines=settings.store_ctx_max_device_lines,
        max_incident_lines=settings.store_ctx_max_incident_lines,
    )
    print(text)
    print(
        f"\n-- {len(snapshot.findings)} findings, {len(snapshot.nodes)} nodes, "
        f"~{estimate_tokens(text)} tokens (budget {settings.store_ctx_snapshot_max_tokens})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
