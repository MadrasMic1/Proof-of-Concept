# app/agentic/store_context/snapshot.py
"""Build one store's snapshot from the database.

Everything is read in ONE read-only transaction at REPEATABLE READ, so every part
reflects the same moment: a GLPI sync landing between two queries cannot make the
incident list disagree with the device status. Rebuilt on every call (no cache).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agentic.config import AgenticSettings
from app.agentic.store_context import queries
from app.agentic.store_context.assemble import StoreSnapshot, assemble_snapshot
from app.agentic.store_context.rows import StoreState
from app.agentic.store_graph.catalogue import Catalogue
from app.core.errors import NotFoundError


async def begin_read_only(session: AsyncSession, timeout_seconds: float) -> None:
    """Start the session's transaction: REPEATABLE READ, read-only, statement timeout."""
    await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    await session.execute(text("SET TRANSACTION READ ONLY"))
    await session.execute(text(f"SET LOCAL statement_timeout = {int(timeout_seconds * 1000)}"))


async def read_store_state(
    session: AsyncSession, store_id: uuid.UUID, *, now: datetime, settings: AgenticSettings
) -> StoreState:
    store = await queries.load_store(session, store_id)
    if store is None:
        raise NotFoundError(f"Store {store_id} was not found.")
    return StoreState(
        store=store,
        db_now=await queries.database_now(session),
        elements=await queries.load_elements(session, store_id),
        devices=await queries.load_devices(session, store_id),
        edges=await queries.load_edges(session, store_id),
        incidents=await queries.load_incidents(
            session, store_id, now, settings.store_ctx_recent_resolved_hours
        ),
        visits=await queries.load_visits(
            session, store_id, now, settings.store_ctx_appointment_lookahead_hours
        ),
        alerts=await queries.load_alerts(session, store_id),
        actions=await queries.load_actions(session, store_id),
        notices=await queries.load_maintenance_notices(session, store_id, now),
    )


async def build_snapshot(
    factory: async_sessionmaker[AsyncSession],
    store_id: uuid.UUID,
    *,
    now: datetime,
    catalogue: Catalogue,
    settings: AgenticSettings,
) -> StoreSnapshot:
    async with factory() as session:
        await begin_read_only(session, settings.store_ctx_query_timeout_seconds)
        try:
            state = await read_store_state(session, store_id, now=now, settings=settings)
        finally:
            await session.rollback()
    return assemble_snapshot(state, catalogue, now=now, settings=settings)
