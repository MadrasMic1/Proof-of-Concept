# app/agentic/config.py
"""Settings of the agentic system, loaded from the same .env as app/core/config.py.

Kept in their own class so app/core/config.py (and tests/test_config.py) stay
unchanged. Every tunable value of the design (Sections 1-12) lives here; change
them in .env, never in code. Values are checked when the settings are created,
so a typo stops the process with a clear message instead of misbehaving later.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from app.agentic.values import EMBEDDING_DIM, KB_ADMIN_ROLE, MCP_ROLE, AgentRole

AgentLlmProvider = Literal["fake", "nvidia", "groq", "foundry"]
FallbackProvider = Literal["none", "fake", "nvidia", "groq", "foundry"]
EmbeddingProvider = Literal["none", "fake", "nvidia", "foundry"]

PLACEHOLDER_SECRETS = frozenset({"", "change_me", "changeme"})
MIN_SECRET_LENGTH = 32


class AgenticSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Same variable as app/core/config.py: the secret rules depend on it.
    environment: Literal["local", "test", "dev", "prod"] = "local"

    # --- Switch (the full kill switch comes in Phase 13) -----------------------------
    agent_enabled: bool = True
    # Confirm also creates the GLPI ticket (false: RIC-only incident, e.g. GLPI not running).
    agent_confirm_to_glpi: bool = True

    # --- Gateway (Section 1) -----------------------------------------------------------
    agent_turn_timeout_seconds: float = Field(default=420.0, ge=10, le=1800)
    agent_app_client_timeout_hint_seconds: float = Field(default=450.0, ge=10, le=1800)
    # Signs the User Context Token. Required (>= 32 characters) when ENVIRONMENT is dev/prod.
    agent_user_context_token_secret: SecretStr | None = None
    agent_user_context_token_issuer: str = "ric-gateway"
    agent_user_context_token_audience: str = "ric-mcp"
    agent_user_context_token_ttl_seconds: int = Field(default=480, ge=30, le=3600)
    agent_token_clock_skew_seconds: int = Field(default=30, ge=0, le=300)
    # POC identity: X-User value -> role, e.g. "priya.store-manager=store_manager,ravi=store_staff".
    # Users not listed are store_staff.
    agent_dev_user_roles: str = ""

    # --- LLM layer (Section 2) ---------------------------------------------------------
    agent_llm_provider: AgentLlmProvider = "fake"
    agent_llm_base_url: str | None = None
    # Empty: LLM_API_KEY of the worker is reused when the provider is the same.
    agent_llm_api_key: SecretStr | None = None
    agent_model_orchestrator: str | None = None
    agent_model_specialist: str | None = None
    agent_model_summariser: str | None = None
    agent_llm_timeout_seconds: float = Field(default=90.0, ge=5, le=600)
    agent_llm_max_retries: int = Field(default=2, ge=0, le=5)
    agent_llm_max_output_tokens: int = Field(default=2048, ge=64, le=32768)
    agent_fallback_provider: FallbackProvider = "none"
    agent_fallback_model: str | None = None
    agent_fallback_api_key: SecretStr | None = None
    agent_fallback_base_url: str | None = None
    agent_fallback_timeout_seconds: float = Field(default=180.0, ge=5, le=600)
    agent_temperature_orchestrator: float = Field(default=0.1, ge=0, le=2)
    agent_temperature_specialist: float = Field(default=0.0, ge=0, le=2)
    # Run tests/agentic/test_agent_live.py (one real specialist run on the model).
    agent_live_test: bool = False

    # --- Session memory (Section 3) ----------------------------------------------------
    agent_session_idle_minutes: int = Field(default=30, ge=1, le=1440)
    agent_memory_verbatim_turns: int = Field(default=6, ge=1, le=50)
    agent_memory_hard_cap_turns: int = Field(default=10, ge=1, le=100)
    agent_memory_summary_max_tokens: int = Field(default=300, ge=50, le=4000)
    agent_message_retention_days: int = Field(default=30, ge=1, le=3650)

    # --- Orchestrator (Section 4) ------------------------------------------------------
    agent_orch_max_model_calls: int = Field(default=12, ge=1, le=100)
    agent_orch_max_specialist_calls: int = Field(default=5, ge=1, le=20)
    agent_orch_max_calls_per_specialist: int = Field(default=3, ge=1, le=20)
    agent_specialist_max_model_calls: int = Field(default=8, ge=1, le=100)
    agent_specialist_timeout_seconds: float = Field(default=240.0, ge=5, le=1800)
    agent_max_suggested_replies: int = Field(default=3, ge=0, le=10)
    agent_tool_timeout_seconds: float = Field(default=30.0, ge=1, le=600)

    # --- Store context and store graph (Section 5) -------------------------------------
    store_ctx_stale_after_minutes: int = Field(default=15, ge=1, le=1440)
    # Off: device statuses are used as stored (what the app screens show). On: a device
    # online/degraded without a heartbeat for STORE_CTX_STALE_AFTER_MINUTES is unknown.
    store_ctx_stale_check: bool = False
    store_ctx_snapshot_max_tokens: int = Field(default=800, ge=100, le=8000)
    store_ctx_query_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60)
    store_ctx_max_device_lines: int = Field(default=8, ge=1, le=100)
    store_ctx_max_incident_lines: int = Field(default=5, ge=1, le=100)
    store_ctx_appointment_lookahead_hours: int = Field(default=48, ge=1, le=720)
    store_ctx_recent_resolved_hours: int = Field(default=24, ge=1, le=720)
    store_graph_catalogue_path: str = "config/element_catalogue.yaml"
    store_graph_max_depth: int = Field(default=6, ge=1, le=20)
    store_graph_common_cause_min: int = Field(default=3, ge=2, le=100)
    store_graph_common_cause_window_minutes: int = Field(default=15, ge=1, le=1440)

    # --- Store Insight (Section 6) -----------------------------------------------------
    store_insight_max_tool_calls: int = Field(default=10, ge=1, le=50)
    store_insight_max_causes: int = Field(default=3, ge=1, le=10)
    store_insight_history_lookback_hours: int = Field(default=6, ge=1, le=24)
    store_insight_flap_threshold: int = Field(default=3, ge=2, le=100)
    store_insight_max_device_options: int = Field(default=3, ge=1, le=10)
    store_insight_validator_repair_attempts: int = Field(default=1, ge=0, le=3)
    store_insight_tool_retry: int = Field(default=1, ge=0, le=3)

    # --- KB Guide (Section 7) and KB ingestion (Section 12) ----------------------------
    kb_search_top_k: int = Field(default=5, ge=1, le=50)
    kb_search_min_score: float = Field(default=0.55, ge=0, le=1)
    kb_search_rrf_k: int = Field(default=60, ge=1, le=1000)
    kb_max_candidates_shown: int = Field(default=3, ge=1, le=10)
    kb_guide_max_clarifications: int = Field(default=2, ge=0, le=10)
    kb_guide_abandon_minutes: int = Field(default=30, ge=1, le=1440)
    kb_guide_max_tool_calls: int = Field(default=6, ge=1, le=50)
    kb_guide_tool_retry: int = Field(default=1, ge=0, le=3)
    kb_articles_path: str = "kb/articles"
    kb_embedding_provider: EmbeddingProvider = "none"
    kb_embedding_base_url: str | None = None
    kb_embedding_api_key: SecretStr | None = None
    kb_embedding_model: str | None = None
    kb_embedding_dim: int = EMBEDDING_DIM
    # Size asked from the embedding API ("dimensions"). NVIDIA models are wider than the
    # 1536-wide column: ask for <= 1536 (1024); shorter vectors are zero-padded (cosine
    # unchanged). Empty: the model's own size (must be <= 1536, e.g. Foundry 3-small).
    kb_embedding_request_dim: int | None = Field(default=None, ge=32, le=EMBEDDING_DIM)
    kb_embedding_timeout_seconds: float = Field(default=60.0, ge=5, le=600)

    @field_validator("kb_embedding_request_dim", mode="before")
    @classmethod
    def _blank_dim_is_none(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    kb_embedding_batch_size: int = Field(default=32, ge=1, le=512)
    kb_instruction_max_chars: int = Field(default=300, ge=50, le=2000)
    kb_ask_max_chars: int = Field(default=120, ge=20, le=500)
    kb_label_max_chars: int = Field(default=30, ge=5, le=100)
    kb_retired_keep_days: int = Field(default=30, ge=1, le=3650)
    # Login with write access to the KB tables (scripts only). User must be ric_kb_admin.
    kb_admin_database_url: str | None = None

    # --- Incident Desk, drafts and confirmations (Section 8) ---------------------------
    incident_draft_ttl_minutes: int = Field(default=15, ge=1, le=1440)
    incident_dup_lookback_hours: int = Field(default=24, ge=1, le=720)
    incident_dup_text_min_score: float = Field(default=0.75, ge=0, le=1)
    incident_impact_high_min_nodes: int = Field(default=5, ge=1, le=1000)
    incident_subject_max_chars: int = Field(default=80, ge=20, le=160)
    incident_description_max_chars: int = Field(default=2000, ge=200, le=20000)
    # How far the model may move impact / urgency (1-3 scale) from the rule baseline.
    incident_impact_max_deviation: int = Field(default=1, ge=0, le=2)
    incident_urgency_max_deviation: int = Field(default=1, ge=0, le=2)
    incident_append_evidence_block: bool = True
    incident_desk_max_tool_calls: int = Field(default=6, ge=1, le=50)
    incident_desk_tool_retry: int = Field(default=1, ge=0, le=3)

    # --- MCP server (Sections 9-11) ----------------------------------------------------
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8100, ge=1, le=65535)
    mcp_url: str = "http://127.0.0.1:8100/mcp"
    # The MCP process's own login and pool. User must be ric_mcp.
    mcp_database_url: str | None = None
    mcp_db_pool_size: int = Field(default=5, ge=1, le=50)
    mcp_db_max_overflow: int = Field(default=5, ge=0, le=50)
    mcp_db_pool_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60)
    mcp_db_statement_timeout_ms: int = Field(default=5000, ge=100, le=600000)
    mcp_tool_timeout_seconds: float = Field(default=30.0, ge=1, le=600)
    mcp_tool_policy_path: str = "config/tool_policy.yaml"
    mcp_rate_limit_per_session_per_minute: int = Field(default=60, ge=1, le=10000)
    mcp_max_identical_calls_per_run: int = Field(default=2, ge=1, le=20)
    mcp_max_drafts_per_session_per_hour: int = Field(default=5, ge=1, le=100)
    mcp_max_findings: int = Field(default=10, ge=1, le=100)
    mcp_max_history_events: int = Field(default=50, ge=1, le=1000)
    mcp_max_incident_results: int = Field(default=10, ge=1, le=100)
    mcp_max_text_field_chars: int = Field(default=1000, ge=100, le=20000)
    mcp_max_public_updates: int = Field(default=3, ge=0, le=20)
    mcp_enable_link_metrics: bool = False

    # --- validators -----------------------------------------------------------------------

    @field_validator(
        "agent_llm_base_url",
        "agent_model_orchestrator",
        "agent_model_specialist",
        "agent_model_summariser",
        "agent_fallback_model",
        "agent_fallback_base_url",
        "kb_embedding_base_url",
        "kb_embedding_model",
        "mcp_database_url",
        "kb_admin_database_url",
    )
    @classmethod
    def _blank_is_none(cls, value: str | None) -> str | None:
        return value.strip() or None if isinstance(value, str) else value

    @field_validator(
        "agent_user_context_token_secret",
        "agent_llm_api_key",
        "agent_fallback_api_key",
        "kb_embedding_api_key",
    )
    @classmethod
    def _blank_secret_is_none(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None or not value.get_secret_value().strip():
            return None
        return value

    @field_validator("mcp_database_url", "kb_admin_database_url")
    @classmethod
    def _require_asyncpg(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("postgresql+asyncpg://"):
            raise ValueError("database URLs must start with postgresql+asyncpg://")
        return value

    @field_validator("kb_embedding_dim")
    @classmethod
    def _fixed_dimension(cls, value: int) -> int:
        if value != EMBEDDING_DIM:
            raise ValueError(
                f"KB_EMBEDDING_DIM must be {EMBEDDING_DIM} (fixed by migration 0005); "
                "another dimension needs a new migration and a full re-ingest"
            )
        return value

    @field_validator("agent_dev_user_roles")
    @classmethod
    def _valid_user_roles(cls, value: str) -> str:
        parse_user_roles(value)
        return value

    @model_validator(mode="after")
    def _check_together(self) -> AgenticSettings:
        if self.agent_app_client_timeout_hint_seconds <= self.agent_turn_timeout_seconds:
            raise ValueError("AGENT_APP_CLIENT_TIMEOUT_HINT_SECONDS must exceed the turn timeout")
        if self.agent_specialist_timeout_seconds >= self.agent_turn_timeout_seconds:
            raise ValueError("AGENT_SPECIALIST_TIMEOUT_SECONDS must be below the turn timeout")
        if self.agent_user_context_token_ttl_seconds <= self.agent_turn_timeout_seconds:
            raise ValueError("AGENT_USER_CONTEXT_TOKEN_TTL_SECONDS must exceed the turn timeout")
        if self.agent_orch_max_calls_per_specialist > self.agent_orch_max_specialist_calls:
            raise ValueError(
                "AGENT_ORCH_MAX_CALLS_PER_SPECIALIST cannot exceed AGENT_ORCH_MAX_SPECIALIST_CALLS"
            )
        if self.agent_orch_max_model_calls <= self.agent_orch_max_specialist_calls:
            raise ValueError(
                "AGENT_ORCH_MAX_MODEL_CALLS must exceed AGENT_ORCH_MAX_SPECIALIST_CALLS "
                "(one model call per specialist call plus the final answer)"
            )
        if self.agent_memory_hard_cap_turns < self.agent_memory_verbatim_turns:
            raise ValueError(
                "AGENT_MEMORY_HARD_CAP_TURNS cannot be below AGENT_MEMORY_VERBATIM_TURNS"
            )
        if self.agent_llm_provider == "foundry" and self.agent_llm_base_url is None:
            raise ValueError("AGENT_LLM_PROVIDER=foundry needs AGENT_LLM_BASE_URL")
        if self.environment in ("dev", "prod"):
            secret = self.agent_user_context_token_secret
            value = secret.get_secret_value() if secret else ""
            if value.strip().lower() in PLACEHOLDER_SECRETS or len(value) < MIN_SECRET_LENGTH:
                raise ValueError(
                    "AGENT_USER_CONTEXT_TOKEN_SECRET must be set (at least "
                    f"{MIN_SECRET_LENGTH} characters, not a placeholder) when ENVIRONMENT is "
                    f"{self.environment}"
                )
        _check_login(self.mcp_database_url, MCP_ROLE, "MCP_DATABASE_URL")
        _check_login(self.kb_admin_database_url, KB_ADMIN_ROLE, "KB_ADMIN_DATABASE_URL")
        return self

    @property
    def user_roles(self) -> dict[str, AgentRole]:
        return parse_user_roles(self.agent_dev_user_roles)

    def role_of(self, user: str) -> AgentRole:
        return self.user_roles.get(user.strip(), AgentRole.STORE_STAFF)


def parse_user_roles(value: str) -> dict[str, AgentRole]:
    """'a=store_manager, b=store_staff' -> {'a': STORE_MANAGER, 'b': STORE_STAFF}."""
    roles: dict[str, AgentRole] = {}
    for part in value.split(","):
        if not part.strip():
            continue
        user, sep, role = part.partition("=")
        if not sep or not user.strip():
            raise ValueError(f"AGENT_DEV_USER_ROLES entry {part.strip()!r} must be user=role")
        try:
            roles[user.strip()] = AgentRole(role.strip())
        except ValueError as exc:
            allowed = ", ".join(r.value for r in AgentRole)
            raise ValueError(
                f"AGENT_DEV_USER_ROLES role {role.strip()!r} is not one of: {allowed}"
            ) from exc
    return roles


def _check_login(url: str | None, role: str, name: str) -> None:
    if url is None:
        return
    user = make_url(url).username
    if user != role:
        raise ValueError(f"{name} must log in as {role} (found {user!r})")


@lru_cache
def get_agentic_settings() -> AgenticSettings:
    return AgenticSettings()
