# scripts/agentic_check_env.py
"""Check the environment of the agentic system (Phase 1), next to scripts.check_env.

    uv run python -m scripts.agentic_check_env

Never prints secrets. Checks: agentic settings, pgvector in both databases, migration
head and table count, the ric_mcp / ric_kb_admin logins and two of their limits.
"""

from __future__ import annotations

import asyncio
import sys
from importlib.metadata import PackageNotFoundError, version

from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.agentic.config import AgenticSettings
from app.core.config import get_settings
from app.db.migrate import alembic_config

EXPECTED_TABLES = 33
PACKAGES = (
    "agent-framework-core",
    "agent-framework-openai",
    "fastmcp",
    "mcp",
    "pyjwt",
    "pgvector",
    "fastapi",
    "pydantic",
    "sqlalchemy",
)
TABLE_COUNT_SQL = text(
    "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' "
    "AND table_type = 'BASE TABLE' AND table_name <> 'alembic_version'"
)

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"[{'OK' if ok else 'FAIL'}] {label}")


def check_packages() -> None:
    for name in PACKAGES:
        try:
            check(True, f"package {name} {version(name)}")
        except PackageNotFoundError:
            check(False, f"package {name} not installed")


async def _scalar(url: str, sql: str) -> object:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            return await connection.scalar(text(sql))
    finally:
        await engine.dispose()


async def _denied(url: str, sql: str) -> bool:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(sql))
                return False
            except DBAPIError as exc:
                return "permission denied" in str(exc).lower()
            finally:
                await transaction.rollback()
    finally:
        await engine.dispose()


async def check_database(url: str, head: str) -> None:
    name = make_url(url).database
    try:
        vector = await _scalar(url, "SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        check(vector is not None, f"{name}: pgvector {vector or 'NOT created'}")
        current = await _scalar(url, "SELECT version_num FROM alembic_version")
        check(current == head, f"{name}: revision {current} (head {head})")
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                tables = await connection.scalar(TABLE_COUNT_SQL)
        finally:
            await engine.dispose()
        check(tables == EXPECTED_TABLES, f"{name}: {tables} tables (expected {EXPECTED_TABLES})")
    except Exception as exc:
        check(False, f"{name}: cannot check ({exc.__class__.__name__}: {exc})")


async def check_login(url: str | None, label: str, denied_sql: str, database: str) -> None:
    if url is None:
        check(False, f"{label}: URL not set in .env")
        return
    target = make_url(url).set(database=database).render_as_string(hide_password=False)
    try:
        user = await _scalar(target, "SELECT current_user")
        check(True, f"{label}: connects to {database} as {user}")
        check(await _denied(target, denied_sql), f"{label}: refused '{denied_sql[:40]}...'")
    except Exception as exc:
        check(False, f"{label}: cannot connect ({exc.__class__.__name__}: {exc})")


async def main() -> int:
    check_packages()
    try:
        agentic = AgenticSettings()
        check(True, f"agentic settings load (environment {agentic.environment})")
    except ValidationError as exc:
        check(False, f"agentic settings: {exc}")
        return 1
    secret = agentic.agent_user_context_token_secret
    check(
        secret is not None and len(secret.get_secret_value()) >= 32,
        "AGENT_USER_CONTEXT_TOKEN_SECRET set (>= 32 characters)",
    )
    settings = get_settings()
    main_url = make_url(settings.database_url)
    test_url = main_url.set(database=f"{main_url.database}_test")
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    for url in (main_url, test_url):
        await check_database(url.render_as_string(hide_password=False), head)
    await check_login(
        agentic.mcp_database_url,
        "ric_mcp",
        "INSERT INTO incidents (id) VALUES (gen_random_uuid())",
        main_url.database,
    )
    await check_login(
        agentic.kb_admin_database_url,
        "ric_kb_admin",
        "SELECT 1 FROM stores LIMIT 1",
        main_url.database,
    )
    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
