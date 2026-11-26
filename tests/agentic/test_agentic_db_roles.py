# tests/agentic/test_agentic_db_roles.py
"""Phase 1: ric_mcp and ric_kb_admin can do exactly what app/agentic/db_roles.py grants.

Runs against <database>_test as the restricted logins (passwords from
MCP_DATABASE_URL / KB_ADMIN_DATABASE_URL). Every statement runs in a transaction
that is rolled back, so nothing is left behind. Skipped when the URLs are not set.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.agentic.config import get_agentic_settings

AGENTIC = get_agentic_settings()

MCP_ALLOWED = [
    "SELECT 1 FROM stores LIMIT 1",
    "SELECT 1 FROM incidents LIMIT 1",
    "SELECT 1 FROM network_element_current_status LIMIT 1",
    "SELECT 1 FROM agent_kb_articles LIMIT 1",
    "SELECT 1 FROM agent_incident_drafts LIMIT 1",
    "UPDATE agent_incident_drafts SET version = version WHERE false",
    "UPDATE agent_kb_guide_sessions SET state_version = state_version WHERE false",
    "INSERT INTO agent_tool_calls (correlation_id, agent, tool, outcome) "
    "VALUES (gen_random_uuid(), 'store_insight', 'phase1.check', 'ok')",
]
MCP_DENIED = [
    "INSERT INTO incidents (id) VALUES (gen_random_uuid())",
    "UPDATE incidents SET description = description WHERE false",
    "DELETE FROM agent_incident_drafts WHERE false",
    "UPDATE audit_log SET actor = actor WHERE false",
    "INSERT INTO agent_kb_articles (kb_ref) VALUES ('KB-X-001')",
    "SELECT 1 FROM api_request_log LIMIT 1",
    "SELECT 1 FROM ai_prompt_versions LIMIT 1",
    "SELECT 1 FROM itsm_inbox LIMIT 1",
]
KB_ADMIN_ALLOWED = [
    "SELECT 1 FROM agent_kb_articles LIMIT 1",
    "DELETE FROM agent_kb_chunks WHERE false",
    "UPDATE agent_kb_articles SET title = title WHERE false",
    "SELECT 1 FROM agent_kb_guide_sessions LIMIT 1",
]
KB_ADMIN_DENIED = [
    "SELECT 1 FROM stores LIMIT 1",
    "SELECT 1 FROM incidents LIMIT 1",
    "UPDATE agent_kb_guide_sessions SET state_version = state_version WHERE false",
    "SELECT 1 FROM agent_incident_drafts LIMIT 1",
]


def _test_url(url: str | None, migrated_db: str) -> str:
    if url is None:
        pytest.skip("MCP_DATABASE_URL / KB_ADMIN_DATABASE_URL not set in .env")
    database = make_url(migrated_db).database
    return make_url(url).set(database=database).render_as_string(hide_password=False)


async def _allowed(url: str, sql: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(sql))
                return True
            except DBAPIError as exc:
                if "permission denied" in str(exc).lower():
                    return False
                raise
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


@pytest.mark.parametrize("sql", MCP_ALLOWED)
async def test_mcp_allowed(migrated_db: str, sql: str) -> None:
    assert await _allowed(_test_url(AGENTIC.mcp_database_url, migrated_db), sql)


@pytest.mark.parametrize("sql", MCP_DENIED)
async def test_mcp_denied(migrated_db: str, sql: str) -> None:
    assert not await _allowed(_test_url(AGENTIC.mcp_database_url, migrated_db), sql)


@pytest.mark.parametrize("sql", KB_ADMIN_ALLOWED)
async def test_kb_admin_allowed(migrated_db: str, sql: str) -> None:
    assert await _allowed(_test_url(AGENTIC.kb_admin_database_url, migrated_db), sql)


@pytest.mark.parametrize("sql", KB_ADMIN_DENIED)
async def test_kb_admin_denied(migrated_db: str, sql: str) -> None:
    assert not await _allowed(_test_url(AGENTIC.kb_admin_database_url, migrated_db), sql)
