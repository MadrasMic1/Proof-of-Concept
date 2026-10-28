"""GLPI sync: GLPI ids on RIC rows, the sync inbox and the sync job state.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-29

GLPI is the ITSM of record; RIC keeps a synchronized copy of its incidents.

- GLPI ids (integers) link RIC rows to GLPI objects:
    stores.glpi_location_id             GLPI Location of the store
    assignment_groups.glpi_group_id     GLPI Group
    network_elements / devices          glpi_itemtype + glpi_item_id (the GLPI asset)
    incidents.glpi_ticket_id            GLPI Ticket
    incident_journal.glpi_followup_id   GLPI follow-up (one journal row per follow-up)
  Every id is unique, so one GLPI object can never be linked to two RIC rows.
  The servicenow_sys_id columns are kept unchanged (not used by the GLPI sync).
- incidents.itsm_updated_at holds GLPI's date_mod of the last applied snapshot
  (a snapshot older than this is ignored); incidents.last_synced_at is when RIC
  applied it.
- itsm_inbox is the sync work queue: one row per GLPI ticket to sync. At most one
  pending row per ticket (ux_itsm_inbox_one_pending), so webhooks that arrive
  together are merged into one sync.
- itsm_sync_state holds scheduled sync jobs (next run, watermark, last result).
- New allowed values: incident_journal.source 'glpi', audit_log.source 'itsm_sync'.

Downgrade removes all of this. Journal rows with source 'glpi' become 'seed' and
audit rows with source 'itsm_sync' become 'worker' (the old value lists).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

GLPI_ITEMTYPES = ("NetworkEquipment", "Computer", "Printer", "Peripheral")
JOURNAL_SOURCES_OLD = ("servicenow", "admin_portal", "mobile_app", "agent", "seed")
JOURNAL_SOURCES_NEW = (*JOURNAL_SOURCES_OLD, "glpi")
AUDIT_SOURCES_OLD = ("admin_portal", "mobile_app", "worker", "processor", "agent", "seed")
AUDIT_SOURCES_NEW = (*AUDIT_SOURCES_OLD, "itsm_sync")

CHECKS_SQL = text(
    """
    SELECT c.conname, pg_get_constraintdef(c.oid)
    FROM pg_constraint c
    JOIN pg_class t ON t.oid = c.conrelid
    WHERE c.contype = 'c' AND t.relname = :table
    """
)


def in_list(values: tuple[str, ...]) -> str:
    return "(" + ", ".join(f"'{value}'" for value in values) + ")"


def asset_columns(table: str) -> str:
    return (
        f"ALTER TABLE {table} "
        "ADD COLUMN glpi_itemtype text "
        f"CONSTRAINT {table}_glpi_itemtype_check "
        f"CHECK (glpi_itemtype IN {in_list(GLPI_ITEMTYPES)}), "
        "ADD COLUMN glpi_item_id integer "
        f"CONSTRAINT {table}_glpi_item_id_check CHECK (glpi_item_id > 0), "
        f"ADD CONSTRAINT {table}_glpi_pair_check "
        "CHECK ((glpi_itemtype IS NULL) = (glpi_item_id IS NULL)), "
        f"ADD CONSTRAINT {table}_glpi_asset_key UNIQUE (glpi_itemtype, glpi_item_id)"
    )


UPGRADE = (
    "ALTER TABLE stores ADD COLUMN glpi_location_id integer "
    "CONSTRAINT stores_glpi_location_id_key UNIQUE "
    "CONSTRAINT stores_glpi_location_id_check CHECK (glpi_location_id > 0)",
    "ALTER TABLE assignment_groups ADD COLUMN glpi_group_id integer "
    "CONSTRAINT assignment_groups_glpi_group_id_key UNIQUE "
    "CONSTRAINT assignment_groups_glpi_group_id_check CHECK (glpi_group_id > 0)",
    asset_columns("network_elements"),
    asset_columns("devices"),
    "ALTER TABLE incidents "
    "ADD COLUMN glpi_ticket_id integer "
    "CONSTRAINT incidents_glpi_ticket_id_key UNIQUE "
    "CONSTRAINT incidents_glpi_ticket_id_check CHECK (glpi_ticket_id > 0), "
    "ADD COLUMN itsm_updated_at timestamptz, "
    "ADD COLUMN last_synced_at timestamptz",
    "ALTER TABLE incident_journal ADD COLUMN glpi_followup_id integer "
    "CONSTRAINT incident_journal_glpi_followup_id_key UNIQUE "
    "CONSTRAINT incident_journal_glpi_followup_id_check CHECK (glpi_followup_id > 0)",
    """
    CREATE TABLE itsm_inbox (
        id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        glpi_ticket_id  integer NOT NULL CHECK (glpi_ticket_id > 0),
        source          text NOT NULL
                        CHECK (source IN ('webhook', 'reconcile', 'link', 'manual')),
        events          jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(events) = 'array'),
        merged_count    integer NOT NULL DEFAULT 1 CHECK (merged_count >= 1),
        status          text NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'processing', 'done', 'ignored',
                                          'conflict', 'failed')),
        attempts        smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
        available_at    timestamptz NOT NULL DEFAULT now(),
        last_error      text,
        received_at     timestamptz NOT NULL DEFAULT now(),
        processed_at    timestamptz,
        updated_at      timestamptz NOT NULL DEFAULT now(),
        CHECK (status NOT IN ('done', 'ignored', 'conflict') OR processed_at IS NOT NULL)
    )
    """,
    "CREATE UNIQUE INDEX ux_itsm_inbox_one_pending ON itsm_inbox (glpi_ticket_id) "
    "WHERE status = 'pending'",
    "CREATE INDEX ix_itsm_inbox_claim ON itsm_inbox (available_at) WHERE status = 'pending'",
    "CREATE INDEX ix_itsm_inbox_ticket ON itsm_inbox (glpi_ticket_id, received_at DESC)",
    "CREATE TRIGGER trg_itsm_inbox_updated BEFORE UPDATE ON itsm_inbox "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE itsm_sync_state (
        job_name              text PRIMARY KEY CHECK (job_name ~ '^[a-z][a-z0-9_]*$'),
        interval_seconds      integer NOT NULL CHECK (interval_seconds > 0),
        next_run_at           timestamptz NOT NULL,
        watermark             timestamptz,
        last_started_at       timestamptz,
        last_success_at       timestamptz,
        consecutive_failures  integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
        last_error            text,
        details               jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(details) = 'object'),
        created_at            timestamptz NOT NULL DEFAULT now(),
        updated_at            timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE TRIGGER trg_itsm_sync_state_updated BEFORE UPDATE ON itsm_sync_state "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
)

DOWNGRADE = (
    "DROP TABLE IF EXISTS itsm_sync_state",
    "DROP TABLE IF EXISTS itsm_inbox",
    "UPDATE audit_log SET source = 'worker' WHERE source = 'itsm_sync'",
    "UPDATE incident_journal SET source = 'seed' WHERE source = 'glpi'",
    "ALTER TABLE incident_journal DROP COLUMN IF EXISTS glpi_followup_id",
    "ALTER TABLE incidents DROP COLUMN IF EXISTS glpi_ticket_id, "
    "DROP COLUMN IF EXISTS itsm_updated_at, DROP COLUMN IF EXISTS last_synced_at",
    "ALTER TABLE devices DROP COLUMN IF EXISTS glpi_itemtype, DROP COLUMN IF EXISTS glpi_item_id",
    "ALTER TABLE network_elements DROP COLUMN IF EXISTS glpi_itemtype, "
    "DROP COLUMN IF EXISTS glpi_item_id",
    "ALTER TABLE assignment_groups DROP COLUMN IF EXISTS glpi_group_id",
    "ALTER TABLE stores DROP COLUMN IF EXISTS glpi_location_id",
)


def replace_value_check(table: str, column: str, values: tuple[str, ...]) -> None:
    """Swap the 'column IN (...)' CHECK of a table for one with the given values.

    The 0001 checks were declared inline, so their names come from PostgreSQL;
    they are found by their definition instead of by name.
    """
    marker = f"({column} = ANY"
    rows = op.get_bind().execute(CHECKS_SQL, {"table": table}).all()
    names = [name for name, definition in rows if marker in definition]
    if len(names) != 1:
        raise RuntimeError(f"expected one value check on {table}.{column}, found {names}")
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {names[0]}")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {table}_{column}_check "
        f"CHECK ({column} IN {in_list(values)})"
    )


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)
    replace_value_check("incident_journal", "source", JOURNAL_SOURCES_NEW)
    replace_value_check("audit_log", "source", AUDIT_SOURCES_NEW)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
    replace_value_check("incident_journal", "source", JOURNAL_SOURCES_OLD)
    replace_value_check("audit_log", "source", AUDIT_SOURCES_OLD)
