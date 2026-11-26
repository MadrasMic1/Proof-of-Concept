# scripts/seed_store_graph.py
"""Load the explicit store-graph edges (seed/data/agent_dependencies.yaml).

    uv run python -m scripts.seed_store_graph               every store
    uv run python -m scripts.seed_store_graph --site 01234  one store

Run after scripts.seed (the stores and inventory must exist). Idempotent: each
store's agent_store_dependencies rows are replaced. The existing seed is untouched.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.agentic.config import get_agentic_settings
from app.agentic.store_graph import load_configured_catalogue
from app.agentic.store_graph.dependencies import load_dependency_template
from app.agentic.store_graph.seed_db import write_store_edges
from app.db.migrate import PROJECT_ROOT
from app.db.session import create_engine_from_settings, create_session_factory, session_scope

SEED = PROJECT_ROOT / "seed" / "data"


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--site", help="only this site code")
    args = parser.parse_args()
    catalogue = load_configured_catalogue(get_agentic_settings())
    template = load_dependency_template(
        SEED / "agent_dependencies.yaml", SEED / "inventory_template.yaml", catalogue
    )
    engine = create_engine_from_settings()
    try:
        async with session_scope(create_session_factory(engine)) as session:
            report = await write_store_edges(session, template, site_code=args.site)
    finally:
        await engine.dispose()
    for site, count in report.edges.items():
        print(f"[OK] {site}: {count} edges")
    for line in report.skipped:
        print(f"[SKIP] {line}")
    print(f"store graph edges written for {len(report.edges)} stores")
    return 0 if report.edges else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
