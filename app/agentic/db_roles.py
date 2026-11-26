# app/agentic/db_roles.py
"""Privileges of the two restricted logins of the agentic system.

    ric_mcp       the MCP server process: reads store, inventory, status, ITSM and
                  agent tables; writes ONLY guide sessions, incident drafts, tool-call
                  rows and audit rows. It can never write incidents.
    ric_kb_admin  the KB ingestion script: reads and writes the two KB tables only.

The statements are DO blocks that do nothing when the role does not exist yet, so
migration 0005 can apply them on any database (dropping a table drops its grants,
which is why the migration, not only scripts.agentic_db_setup, grants them).
"""

from __future__ import annotations

from app.agentic.values import KB_ADMIN_ROLE, MCP_ROLE

# Existing tables the MCP process reads (migrations 0001-0004).
MCP_READ_TABLES: tuple[str, ...] = (
    "stores",
    "assignment_groups",
    "network_element_types",
    "device_types",
    "network_elements",
    "devices",
    "network_events",
    "network_element_current_status",
    "incidents",
    "incident_journal",
    "appointments",
    "notifications",
    "notification_targets",
    "store_alerts",
    "store_actions",
    "audit_log",
)

# Tables of migration 0005, in creation order.
AGENT_TABLES: tuple[str, ...] = (
    "agent_store_dependencies",
    "agent_chat_sessions",
    "agent_chat_messages",
    "agent_kb_articles",
    "agent_kb_chunks",
    "agent_kb_guide_sessions",
    "agent_incident_drafts",
    "agent_llm_calls",
    "agent_tool_calls",
)

MCP_INSERT_UPDATE_TABLES: tuple[str, ...] = ("agent_kb_guide_sessions", "agent_incident_drafts")
MCP_INSERT_ONLY_TABLES: tuple[str, ...] = ("agent_tool_calls", "audit_log")
MCP_IDENTITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("audit_log", "id"),
    ("agent_tool_calls", "id"),
)

KB_ADMIN_WRITE_TABLES: tuple[str, ...] = ("agent_kb_articles", "agent_kb_chunks")
KB_ADMIN_READ_TABLES: tuple[str, ...] = ("agent_kb_guide_sessions",)


def _quote(sql: str) -> str:
    return "'" + sql.replace("'", "''") + "'"


def _guarded(role: str, lines: list[str]) -> str:
    """One DO block that runs the lines only when the role exists."""
    body = "\n".join(f"        {line}" for line in lines)
    return (
        "DO $$\n"
        "BEGIN\n"
        f"    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN\n"
        f"{body}\n"
        "    END IF;\n"
        "END\n"
        "$$"
    )


def _execute(sql: str) -> str:
    return f"EXECUTE {_quote(sql)};"


def _sequence(action: str, table: str, column: str, role: str) -> str:
    word = "TO" if action == "GRANT" else "FROM"
    return (
        f"EXECUTE '{action} USAGE ON SEQUENCE ' || "
        f"pg_get_serial_sequence('{table}', '{column}') || ' {word} {role}';"
    )


def grant_statements() -> list[str]:
    """Grants for both roles. Needs migration 0005's tables to exist."""
    mcp = [
        _execute(f"GRANT SELECT ON {', '.join(MCP_READ_TABLES)} TO {MCP_ROLE}"),
        _execute(f"GRANT SELECT ON {', '.join(AGENT_TABLES)} TO {MCP_ROLE}"),
        _execute(f"GRANT INSERT, UPDATE ON {', '.join(MCP_INSERT_UPDATE_TABLES)} TO {MCP_ROLE}"),
        _execute(f"GRANT INSERT ON {', '.join(MCP_INSERT_ONLY_TABLES)} TO {MCP_ROLE}"),
        *(_sequence("GRANT", t, c, MCP_ROLE) for t, c in MCP_IDENTITY_COLUMNS),
    ]
    kb_admin = [
        _execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(KB_ADMIN_WRITE_TABLES)} "
            f"TO {KB_ADMIN_ROLE}"
        ),
        _execute(f"GRANT SELECT ON {', '.join(KB_ADMIN_READ_TABLES)} TO {KB_ADMIN_ROLE}"),
    ]
    return [_guarded(MCP_ROLE, mcp), _guarded(KB_ADMIN_ROLE, kb_admin)]


def revoke_statements() -> list[str]:
    """Revokes on the EXISTING tables (the agent tables are dropped with their grants)."""
    mcp = [
        _execute(f"REVOKE ALL ON {', '.join(MCP_READ_TABLES)} FROM {MCP_ROLE}"),
        _sequence("REVOKE", "audit_log", "id", MCP_ROLE),
    ]
    return [_guarded(MCP_ROLE, mcp)]
