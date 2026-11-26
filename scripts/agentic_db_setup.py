# scripts/agentic_db_setup.py
"""Prepare PostgreSQL for the agentic system (Phase 1). Idempotent: safe to run again.

    uv run python -m scripts.agentic_db_setup           do everything
    uv run python -m scripts.agentic_db_setup --check   only report, change nothing

For the main database and <name>_test (like scripts.db_bootstrap), as the superuser
in ADMIN_DATABASE_URL:
    1. CREATE EXTENSION vector (pgvector must be installed on the server: manual step)
    2. create / update the logins ric_mcp and ric_kb_admin (passwords from
       MCP_DATABASE_URL and KB_ADMIN_DATABASE_URL); no superuser, no createdb/createrole
    3. when migration 0005's tables exist: apply their grants (app/agentic/db_roles.py)
Migration 0005 applies the same grants itself; step 3 covers a database migrated
before the logins existed.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys

import asyncpg
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from app.agentic.db_roles import grant_statements
from app.agentic.values import KB_ADMIN_ROLE, MCP_ROLE

_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")


class SetupSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    admin_database_url: str
    database_url: str
    mcp_database_url: str | None = None
    kb_admin_database_url: str | None = None


def _ident(name: str) -> str:
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"Unsafe identifier {name!r}: use lowercase letters, digits and _")
    return f'"{name}"'


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _connect_kwargs(url: URL, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host or "localhost",
        "port": url.port or 5432,
        "database": database,
    }


def _login(url_text: str | None, expected: str, name: str) -> tuple[str, str] | None:
    if not url_text or not url_text.strip():
        return None
    url = make_url(url_text)
    if url.username != expected or not url.password:
        raise ValueError(f"{name} must log in as {expected} with a password")
    return expected, url.password


async def _ensure_role(conn: asyncpg.Connection, role: str, password: str, check: bool) -> None:
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    if check:
        print(f"[{'OK' if exists else 'MISSING'}] role {role}")
        return
    verb = "ALTER" if exists else "CREATE"
    await conn.execute(
        f"{verb} ROLE {_ident(role)} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
        f"PASSWORD {_literal(password)}"
    )
    print(f"[OK] role {role}: {'exists, password synced' if exists else 'created'}")


async def _prepare_database(admin: URL, database: str, check: bool) -> bool:
    conn = await asyncpg.connect(**_connect_kwargs(admin, database))
    try:
        available = await conn.fetchrow(
            "SELECT default_version, installed_version FROM pg_available_extensions "
            "WHERE name = 'vector'"
        )
        if available is None:
            print(
                f"[FAIL] {database}: pgvector is not installed on this PostgreSQL server "
                "(see MANUAL STEP A of the Phase 1 prompts)"
            )
            return False
        if check:
            state = available["installed_version"] or "not created"
            print(f"[OK] {database}: pgvector {available['default_version']} available ({state})")
        else:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            version = await conn.fetchval(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            )
            print(f"[OK] {database}: extension vector {version}")
        has_tables = await conn.fetchval("SELECT to_regclass('agent_tool_calls') IS NOT NULL")
        if has_tables and not check:
            for statement in grant_statements():
                await conn.execute(statement)
            print(f"[OK] {database}: grants applied for {MCP_ROLE} and {KB_ADMIN_ROLE}")
        elif not has_tables:
            print(f"[INFO] {database}: migration 0005 not applied yet (grants come with it)")
        return True
    finally:
        await conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="report only, change nothing")
    args = parser.parse_args()

    settings = SetupSettings()
    admin = make_url(settings.admin_database_url)
    main_db = make_url(settings.database_url).database
    if not main_db:
        print("DATABASE_URL must include the database name", file=sys.stderr)
        return 1
    try:
        logins = [
            _login(settings.mcp_database_url, MCP_ROLE, "MCP_DATABASE_URL"),
            _login(settings.kb_admin_database_url, KB_ADMIN_ROLE, "KB_ADMIN_DATABASE_URL"),
        ]
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1
    if any(login is None for login in logins) and not args.check:
        print(
            "[FAIL] MCP_DATABASE_URL and KB_ADMIN_DATABASE_URL must be set in .env (MANUAL STEP B)",
            file=sys.stderr,
        )
        return 1

    conn = await asyncpg.connect(**_connect_kwargs(admin, "postgres"))
    try:
        for role in (MCP_ROLE, KB_ADMIN_ROLE):
            login = next((item for item in logins if item and item[0] == role), None)
            await _ensure_role(conn, role, login[1] if login else "", args.check)
    finally:
        await conn.close()

    ok = True
    for database in (main_db, f"{main_db}_test"):
        ok = await _prepare_database(admin, database, args.check) and ok
    print("agentic database setup " + ("checked" if args.check else "complete"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
