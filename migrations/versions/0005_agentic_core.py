"""Agentic core: store graph edges, KB, guide sessions, drafts, chat and agent logs.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-09

Nine tables, all in public with the agent_ prefix (the model and migration tests
compare the public schema with Base.metadata):

    agent_store_dependencies  graph edges the inventory columns do not describe
                              (redundancy groups, service dependencies); the uplink
                              columns of network_elements / devices are used as edges too
    agent_chat_sessions       one conversation; cached conversation state and summary
    agent_chat_messages      user and assistant turns; client_message_id dedupes retries
    agent_kb_articles         versioned structured articles (one published per kb_ref)
    agent_kb_chunks           search text, pgvector embedding, generated tsvector
    agent_kb_guide_sessions   guided troubleshooting state (one in progress per chat)
    agent_incident_drafts     drafts shaped like incidents (one pending per chat);
                              incident_id is set once, on Confirm
    agent_llm_calls           every agent model call (tokens, latency, fallback)
    agent_tool_calls          every MCP tool call (outcome, policy version)

Composite (id, store_id) foreign keys keep every row inside one store, as in 0001.
Needs the pgvector extension (created by scripts.agentic_db_setup, which needs a
superuser). Grants for ric_mcp / ric_kb_admin are applied when those roles exist
(app/agentic/db_roles.py). Downgrade drops the tables and revokes the grants on the
existing tables; the extension and the roles are left in place.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from app.agentic.db_roles import grant_statements, revoke_statements

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = "('store_staff', 'store_manager')"
SPECIALISTS = "('store_insight', 'kb_guide', 'incident_desk')"
SLUG = "'^[a-z][a-z0-9_]*$'"

UPGRADE = (
    # --- Store graph -----------------------------------------------------------------
    f"""
    CREATE TABLE agent_store_dependencies (
        id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        store_id          uuid NOT NULL REFERENCES stores(id),
        from_kind         text NOT NULL CHECK (from_kind IN ('device', 'network_element')),
        from_device_id    uuid,
        from_element_id   uuid,
        to_kind           text NOT NULL CHECK (to_kind IN ('device', 'network_element', 'service')),
        to_device_id      uuid,
        to_element_id     uuid,
        to_service        text CHECK (to_service ~ {SLUG}),
        relation          text NOT NULL
                          CHECK (relation IN ('connects_via', 'powered_by', 'uses_service')),
        redundancy_group  text CHECK (redundancy_group ~ {SLUG}),
        note              text,
        created_at        timestamptz NOT NULL DEFAULT now(),
        updated_at        timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (from_device_id, store_id) REFERENCES devices (id, store_id),
        FOREIGN KEY (from_element_id, store_id) REFERENCES network_elements (id, store_id),
        FOREIGN KEY (to_device_id, store_id) REFERENCES devices (id, store_id),
        FOREIGN KEY (to_element_id, store_id) REFERENCES network_elements (id, store_id),
        CHECK ((from_kind = 'device') = (from_device_id IS NOT NULL)),
        CHECK ((from_kind = 'network_element') = (from_element_id IS NOT NULL)),
        CHECK ((to_kind = 'device') = (to_device_id IS NOT NULL)),
        CHECK ((to_kind = 'network_element') = (to_element_id IS NOT NULL)),
        CHECK ((to_kind = 'service') = (to_service IS NOT NULL)),
        CHECK (from_device_id IS NULL OR from_device_id IS DISTINCT FROM to_device_id),
        CHECK (from_element_id IS NULL OR from_element_id IS DISTINCT FROM to_element_id)
    )
    """,
    "CREATE UNIQUE INDEX ux_dep_edge ON agent_store_dependencies (store_id, from_kind, "
    "coalesce(from_device_id, from_element_id), to_kind, "
    "coalesce(to_device_id::text, to_element_id::text, to_service), relation)",
    "CREATE INDEX ix_dep_store ON agent_store_dependencies (store_id)",
    "CREATE TRIGGER trg_dep_updated BEFORE UPDATE ON agent_store_dependencies "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    # --- Chat sessions and messages --------------------------------------------------
    f"""
    CREATE TABLE agent_chat_sessions (
        id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        store_id           uuid NOT NULL REFERENCES stores(id),
        user_id            text NOT NULL CHECK (length(trim(user_id)) > 0),
        role               text NOT NULL CHECK (role IN {ROLES}),
        status             text NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'expired', 'closed')),
        state              text NOT NULL DEFAULT 'idle'
                           CHECK (state IN ('idle', 'guiding', 'guide_resolved',
                                            'guide_exhausted', 'draft_pending',
                                            'incident_raised')),
        state_data         jsonb NOT NULL DEFAULT '{{}}'
                           CHECK (jsonb_typeof(state_data) = 'object'),
        state_version      integer NOT NULL DEFAULT 1 CHECK (state_version > 0),
        summary            text,
        summary_upto_turn  integer NOT NULL DEFAULT 0 CHECK (summary_upto_turn >= 0),
        turn_count         integer NOT NULL DEFAULT 0 CHECK (turn_count >= 0),
        last_activity_at   timestamptz NOT NULL DEFAULT now(),
        closed_at          timestamptz,
        created_at         timestamptz NOT NULL DEFAULT now(),
        updated_at         timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, store_id),
        CHECK ((status = 'active') = (closed_at IS NULL)),
        CHECK (summary_upto_turn <= turn_count)
    )
    """,
    "CREATE UNIQUE INDEX ux_chat_one_active ON agent_chat_sessions (store_id, user_id) "
    "WHERE status = 'active'",
    "CREATE INDEX ix_chat_idle ON agent_chat_sessions (last_activity_at) WHERE status = 'active'",
    "CREATE TRIGGER trg_chat_sessions_updated BEFORE UPDATE ON agent_chat_sessions "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE agent_chat_messages (
        id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        session_id         uuid NOT NULL REFERENCES agent_chat_sessions(id),
        store_id           uuid NOT NULL REFERENCES stores(id),
        turn_no            integer NOT NULL CHECK (turn_no > 0),
        role               text NOT NULL CHECK (role IN ('user', 'assistant')),
        content            text NOT NULL,
        response           jsonb CHECK (response IS NULL OR jsonb_typeof(response) = 'object'),
        client_message_id  uuid,
        correlation_id     uuid NOT NULL,
        created_at         timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (session_id, store_id) REFERENCES agent_chat_sessions (id, store_id),
        UNIQUE (session_id, turn_no, role),
        CHECK ((role = 'assistant') = (response IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX ux_chat_msg_client ON agent_chat_messages (session_id, client_message_id) "
    "WHERE client_message_id IS NOT NULL",
    "CREATE INDEX ix_chat_msg_created ON agent_chat_messages (created_at)",
    # --- Knowledge base --------------------------------------------------------------
    """
    CREATE TABLE agent_kb_articles (
        id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        kb_ref        text NOT NULL CHECK (kb_ref ~ '^KB-[A-Z0-9]+-[0-9]{3}$'),
        version       integer NOT NULL CHECK (version > 0),
        status        text NOT NULL DEFAULT 'published' CHECK (status IN ('published', 'retired')),
        title         text NOT NULL,
        summary       text NOT NULL,
        applies_to    text[] NOT NULL CHECK (cardinality(applies_to) > 0),
        fault_codes   text[] NOT NULL DEFAULT '{}',
        kb_tags       text[] NOT NULL DEFAULT '{}',
        audience      text NOT NULL DEFAULT 'store_staff'
                      CHECK (audience IN ('store_staff', 'technician')),
        content       jsonb NOT NULL CHECK (jsonb_typeof(content) = 'object'),
        content_hash  text NOT NULL,
        published_at  timestamptz NOT NULL DEFAULT now(),
        retired_at    timestamptz,
        created_at    timestamptz NOT NULL DEFAULT now(),
        updated_at    timestamptz NOT NULL DEFAULT now(),
        UNIQUE (kb_ref, version),
        CHECK ((status = 'retired') = (retired_at IS NOT NULL))
    )
    """,
    "CREATE UNIQUE INDEX ux_kb_one_published ON agent_kb_articles (kb_ref) "
    "WHERE status = 'published'",
    "CREATE INDEX ix_kb_tags ON agent_kb_articles USING gin (kb_tags)",
    "CREATE INDEX ix_kb_applies_to ON agent_kb_articles USING gin (applies_to)",
    "CREATE INDEX ix_kb_fault_codes ON agent_kb_articles USING gin (fault_codes)",
    "CREATE TRIGGER trg_kb_articles_updated BEFORE UPDATE ON agent_kb_articles "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    """
    CREATE TABLE agent_kb_chunks (
        id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        article_id       uuid NOT NULL REFERENCES agent_kb_articles(id) ON DELETE CASCADE,
        chunk_type       text NOT NULL CHECK (chunk_type IN ('article', 'step')),
        step_id          text,
        chunk_text       text NOT NULL CHECK (length(trim(chunk_text)) > 0),
        text_hash        text NOT NULL,
        embedding        vector(1536),
        embedding_model  text,
        tsv              tsvector GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED,
        created_at       timestamptz NOT NULL DEFAULT now(),
        CHECK ((chunk_type = 'step') = (step_id IS NOT NULL)),
        CHECK ((embedding IS NULL) = (embedding_model IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX ux_kb_chunk ON agent_kb_chunks (article_id, chunk_type, "
    "coalesce(step_id, ''))",
    "CREATE INDEX ix_kb_chunk_embedding ON agent_kb_chunks "
    "USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_kb_chunk_tsv ON agent_kb_chunks USING gin (tsv)",
    # --- Guided troubleshooting -----------------------------------------------------
    """
    CREATE TABLE agent_kb_guide_sessions (
        id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        chat_session_id     uuid NOT NULL REFERENCES agent_chat_sessions(id),
        store_id            uuid NOT NULL REFERENCES stores(id),
        user_id             text NOT NULL,
        article_id          uuid NOT NULL REFERENCES agent_kb_articles(id),
        kb_ref              text NOT NULL,
        kb_version          integer NOT NULL CHECK (kb_version > 0),
        subject_device_id   uuid,
        subject_element_id  uuid,
        finding_ids         text[] NOT NULL DEFAULT '{}',
        current_step        text NOT NULL,
        status              text NOT NULL DEFAULT 'in_progress'
                            CHECK (status IN ('in_progress', 'resolved', 'exhausted',
                                              'needs_technician', 'abandoned')),
        clarifications      smallint NOT NULL DEFAULT 0 CHECK (clarifications >= 0),
        history             jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(history) = 'array'),
        state_version       integer NOT NULL DEFAULT 1 CHECK (state_version > 0),
        ended_at            timestamptz,
        created_at          timestamptz NOT NULL DEFAULT now(),
        updated_at          timestamptz NOT NULL DEFAULT now(),
        UNIQUE (id, store_id),
        FOREIGN KEY (chat_session_id, store_id) REFERENCES agent_chat_sessions (id, store_id),
        FOREIGN KEY (subject_device_id, store_id) REFERENCES devices (id, store_id),
        FOREIGN KEY (subject_element_id, store_id) REFERENCES network_elements (id, store_id),
        CHECK (num_nonnulls(subject_device_id, subject_element_id) <= 1),
        CHECK ((status = 'in_progress') = (ended_at IS NULL))
    )
    """,
    "CREATE UNIQUE INDEX ux_guide_one_active ON agent_kb_guide_sessions (chat_session_id) "
    "WHERE status = 'in_progress'",
    "CREATE INDEX ix_guide_article ON agent_kb_guide_sessions (article_id)",
    "CREATE TRIGGER trg_guide_sessions_updated BEFORE UPDATE ON agent_kb_guide_sessions "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    # --- Incident drafts (fields follow the incidents table) ------------------------
    """
    CREATE TABLE agent_incident_drafts (
        id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        chat_session_id             uuid NOT NULL REFERENCES agent_chat_sessions(id),
        store_id                    uuid NOT NULL REFERENCES stores(id),
        user_id                     text NOT NULL,
        guide_session_id            uuid,
        status                      text NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending', 'confirmed', 'cancelled',
                                                      'expired', 'superseded', 'failed')),
        version                     integer NOT NULL DEFAULT 1 CHECK (version > 0),
        short_description           text NOT NULL CHECK (length(trim(short_description)) > 0),
        description                 text NOT NULL CHECK (length(trim(description)) > 0),
        u_symptom                   text,
        category                    text NOT NULL
                                    CHECK (category IN ('network', 'hardware', 'software',
                                                        'inquiry')),
        subcategory                 text,
        impact                      smallint NOT NULL CHECK (impact BETWEEN 1 AND 3),
        urgency                     smallint NOT NULL CHECK (urgency BETWEEN 1 AND 3),
        priority                    smallint GENERATED ALWAYS AS (impact + urgency - 1) STORED,
        baseline_impact             smallint NOT NULL CHECK (baseline_impact BETWEEN 1 AND 3),
        baseline_urgency            smallint NOT NULL CHECK (baseline_urgency BETWEEN 1 AND 3),
        reasons                     jsonb NOT NULL DEFAULT '{}'
                                    CHECK (jsonb_typeof(reasons) = 'object'),
        assignment_group_id         uuid REFERENCES assignment_groups(id),
        cmdb_ci_network_element_id  uuid,
        cmdb_ci_device_id           uuid,
        impacted_nodes              jsonb NOT NULL DEFAULT '[]'
                                    CHECK (jsonb_typeof(impacted_nodes) = 'array'),
        finding_ids                 text[] NOT NULL DEFAULT '{}',
        evidence                    jsonb NOT NULL DEFAULT '{}'
                                    CHECK (jsonb_typeof(evidence) = 'object'),
        steps_tried                 jsonb NOT NULL DEFAULT '[]'
                                    CHECK (jsonb_typeof(steps_tried) = 'array'),
        duplicate_check             jsonb NOT NULL DEFAULT '{}'
                                    CHECK (jsonb_typeof(duplicate_check) = 'object'),
        incident_id                 uuid UNIQUE,
        last_error                  text,
        expires_at                  timestamptz NOT NULL,
        confirmed_at                timestamptz,
        created_at                  timestamptz NOT NULL DEFAULT now(),
        updated_at                  timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY (chat_session_id, store_id) REFERENCES agent_chat_sessions (id, store_id),
        FOREIGN KEY (guide_session_id, store_id) REFERENCES agent_kb_guide_sessions (id, store_id),
        FOREIGN KEY (cmdb_ci_network_element_id, store_id)
            REFERENCES network_elements (id, store_id),
        FOREIGN KEY (cmdb_ci_device_id, store_id) REFERENCES devices (id, store_id),
        FOREIGN KEY (incident_id, store_id) REFERENCES incidents (id, store_id),
        CHECK (num_nonnulls(cmdb_ci_network_element_id, cmdb_ci_device_id) <= 1),
        CHECK ((status = 'confirmed') = (incident_id IS NOT NULL AND confirmed_at IS NOT NULL)),
        CHECK (expires_at > created_at)
    )
    """,
    "CREATE UNIQUE INDEX ux_draft_one_pending ON agent_incident_drafts (chat_session_id) "
    "WHERE status = 'pending'",
    "CREATE INDEX ix_draft_expiry ON agent_incident_drafts (expires_at) WHERE status = 'pending'",
    "CREATE TRIGGER trg_incident_drafts_updated BEFORE UPDATE ON agent_incident_drafts "
    "FOR EACH ROW EXECUTE FUNCTION set_updated_at()",
    # --- Agent logs ------------------------------------------------------------------
    """
    CREATE TABLE agent_llm_calls (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        occurred_at        timestamptz NOT NULL DEFAULT now(),
        correlation_id     uuid NOT NULL,
        chat_session_id    uuid REFERENCES agent_chat_sessions(id),
        store_id           uuid REFERENCES stores(id),
        user_id            text,
        agent              text NOT NULL CHECK (agent IN ('orchestrator', 'store_insight',
                                                          'kb_guide', 'incident_desk',
                                                          'summariser')),
        profile            text NOT NULL
                           CHECK (profile IN ('orchestrator', 'specialist', 'summariser')),
        provider           text NOT NULL,
        model              text NOT NULL,
        fallback_used      boolean NOT NULL DEFAULT false,
        status             text NOT NULL CHECK (status IN ('succeeded', 'failed', 'refused')),
        prompt_tokens      integer CHECK (prompt_tokens >= 0),
        completion_tokens  integer CHECK (completion_tokens >= 0),
        latency_ms         integer CHECK (latency_ms >= 0),
        error_code         text
    )
    """,
    "CREATE INDEX ix_llm_store_time ON agent_llm_calls (store_id, occurred_at DESC)",
    "CREATE INDEX ix_llm_session ON agent_llm_calls (chat_session_id) "
    "WHERE chat_session_id IS NOT NULL",
    "CREATE INDEX ix_llm_correlation ON agent_llm_calls (correlation_id)",
    f"""
    CREATE TABLE agent_tool_calls (
        id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        occurred_at        timestamptz NOT NULL DEFAULT now(),
        correlation_id     uuid NOT NULL,
        chat_session_id    uuid REFERENCES agent_chat_sessions(id),
        store_id           uuid REFERENCES stores(id),
        user_id            text,
        role               text CHECK (role IN {ROLES}),
        agent              text CHECK (agent IN {SPECIALISTS}),
        tool               text NOT NULL,
        outcome            text NOT NULL CHECK (outcome IN ('ok', 'not_found', 'forbidden',
                                   'invalid_input', 'conflict', 'timeout', 'unavailable',
                                   'disabled', 'rate_limited', 'invalid_token',
                                   'policy_mismatch', 'error')),
        duration_ms        integer CHECK (duration_ms >= 0),
        args_hash          text,
        policy_version     text,
        catalogue_version  text,
        error_message      text
    )
    """,
    "CREATE INDEX ix_tool_store_time ON agent_tool_calls (store_id, occurred_at DESC)",
    "CREATE INDEX ix_tool_correlation ON agent_tool_calls (correlation_id)",
    "CREATE INDEX ix_tool_denied ON agent_tool_calls (occurred_at DESC) WHERE outcome <> 'ok'",
)

DROP_ORDER = (
    "agent_tool_calls",
    "agent_llm_calls",
    "agent_incident_drafts",
    "agent_kb_guide_sessions",
    "agent_kb_chunks",
    "agent_kb_articles",
    "agent_chat_messages",
    "agent_chat_sessions",
    "agent_store_dependencies",
)


def _require_vector() -> None:
    found = op.get_bind().execute(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
    if found.scalar() is None:
        raise RuntimeError(
            "The pgvector extension is not created in this database. "
            "Run: uv run python -m scripts.agentic_db_setup"
        )


def upgrade() -> None:
    _require_vector()
    for statement in UPGRADE:
        op.execute(statement)
    for statement in grant_statements():
        op.execute(statement)


def downgrade() -> None:
    for statement in revoke_statements():
        op.execute(statement)
    for table in DROP_ORDER:
        op.execute(f"DROP TABLE IF EXISTS {table}")
