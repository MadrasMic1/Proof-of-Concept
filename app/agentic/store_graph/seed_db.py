# app/agentic/store_graph/seed_db.py
"""Write the explicit store-graph edges for every store in the database.

For each store: the edges of the template are resolved by element NAME (the
template's name pattern with the store's site code) and by device type, then the
store's agent_store_dependencies rows are replaced in the caller's transaction.
Idempotent: running it again gives the same rows. Stores without inventory are
skipped (reported, not an error).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.store_graph.dependencies import DependencyTemplate
from app.models import AgentStoreDependency, Device, NetworkElement, Store

PREFERRED_NOTE = "preferred"


@dataclass
class EdgeReport:
    edges: dict[str, int] = field(default_factory=dict)  # site -> rows written
    skipped: list[str] = field(default_factory=list)


async def write_store_edges(
    session: AsyncSession, template: DependencyTemplate, *, site_code: str | None = None
) -> EdgeReport:
    report = EdgeReport()
    query = select(Store.id, Store.site_code).order_by(Store.site_code)
    if site_code is not None:
        query = query.where(Store.site_code == site_code)
    for store_id, site in (await session.execute(query)).all():
        elements = dict(
            (
                await session.execute(
                    select(NetworkElement.name, NetworkElement.id).where(
                        NetworkElement.store_id == store_id, NetworkElement.is_active
                    )
                )
            ).all()
        )
        if not elements:
            report.skipped.append(f"{site}: no network elements")
            continue
        rows: list[dict[str, object]] = []
        for spec in template.element_edges:
            source = elements.get(template.element_name(spec.from_key, site))
            target = elements.get(template.element_name(spec.to_key, site))
            if source is None or target is None:
                report.skipped.append(f"{site}: {spec.from_key} -> {spec.to_key} (element missing)")
                continue
            rows.append(
                {
                    "id": uuid.uuid4(),
                    "store_id": store_id,
                    "from_kind": "network_element",
                    "from_element_id": source,
                    "to_kind": "network_element",
                    "to_element_id": target,
                    "relation": spec.relation,
                    "redundancy_group": spec.redundancy_group,
                    "note": PREFERRED_NOTE if spec.preferred else None,
                }
            )
        for service in template.device_services:
            device_ids = (
                await session.scalars(
                    select(Device.id).where(
                        Device.store_id == store_id,
                        Device.device_type_code == service.device_type,
                        Device.is_active,
                    )
                )
            ).all()
            rows += [
                {
                    "id": uuid.uuid4(),
                    "store_id": store_id,
                    "from_kind": "device",
                    "from_device_id": device_id,
                    "to_kind": "service",
                    "to_service": service.service,
                    "relation": "uses_service",
                }
                for device_id in device_ids
            ]
        await session.execute(
            delete(AgentStoreDependency).where(AgentStoreDependency.store_id == store_id)
        )
        for row in rows:
            await session.execute(insert(AgentStoreDependency).values(**row))
        report.edges[site] = len(rows)
    return report
