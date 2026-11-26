# tests/agentic/test_agentic_config.py
"""Phase 1: agentic settings and the tool result envelope (no database)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.agentic.common import NodeKind, NodeRef, failure, success
from app.agentic.config import AgenticSettings, parse_user_roles
from app.agentic.values import AgentRole, ErrorCode

MCP_URL = "postgresql+asyncpg://ric_mcp:pw@localhost:5432/ric_poc"
KB_URL = "postgresql+asyncpg://ric_kb_admin:pw@localhost:5432/ric_poc"
GOOD_SECRET = "x" * 40


def _settings(**values: object) -> AgenticSettings:
    values.setdefault("environment", "local")
    return AgenticSettings(_env_file=None, **values)


def test_defaults_are_valid() -> None:
    s = _settings()
    assert s.agent_turn_timeout_seconds == 420
    assert s.agent_orch_max_specialist_calls == 5
    assert s.agent_orch_max_calls_per_specialist == 3
    assert s.agent_user_context_token_ttl_seconds == 480
    assert s.kb_embedding_dim == 1536
    assert s.agent_llm_provider == "fake"
    assert s.mcp_database_url is None


def test_blank_values_are_none() -> None:
    s = _settings(mcp_database_url="  ", agent_llm_api_key="", agent_model_specialist=" ")
    assert s.mcp_database_url is None
    assert s.agent_llm_api_key is None
    assert s.agent_model_specialist is None


def test_database_urls_need_asyncpg_and_the_right_login() -> None:
    s = _settings(mcp_database_url=MCP_URL, kb_admin_database_url=KB_URL)
    assert s.mcp_database_url == MCP_URL
    with pytest.raises(ValidationError, match="asyncpg"):
        _settings(mcp_database_url="postgresql://ric_mcp:pw@localhost/ric_poc")
    with pytest.raises(ValidationError, match="ric_mcp"):
        _settings(mcp_database_url="postgresql+asyncpg://ric_app:pw@localhost/ric_poc")
    with pytest.raises(ValidationError, match="ric_kb_admin"):
        _settings(kb_admin_database_url=MCP_URL)


def test_secret_required_outside_local_and_test() -> None:
    assert _settings(environment="local").agent_user_context_token_secret is None
    for bad in (None, "change_me", "short-secret"):
        with pytest.raises(ValidationError, match="AGENT_USER_CONTEXT_TOKEN_SECRET"):
            _settings(environment="prod", agent_user_context_token_secret=bad)
    ok = _settings(environment="dev", agent_user_context_token_secret=GOOD_SECRET)
    assert ok.agent_user_context_token_secret.get_secret_value() == GOOD_SECRET


def test_timeouts_and_caps_must_fit_together() -> None:
    with pytest.raises(ValidationError, match="APP_CLIENT_TIMEOUT"):
        _settings(agent_turn_timeout_seconds=500, agent_user_context_token_ttl_seconds=600)
    with pytest.raises(ValidationError, match="SPECIALIST_TIMEOUT"):
        _settings(agent_specialist_timeout_seconds=420)
    with pytest.raises(ValidationError, match="TOKEN_TTL"):
        _settings(agent_user_context_token_ttl_seconds=400)
    with pytest.raises(ValidationError, match="PER_SPECIALIST"):
        _settings(agent_orch_max_calls_per_specialist=6)
    with pytest.raises(ValidationError, match="MAX_MODEL_CALLS"):
        _settings(agent_orch_max_model_calls=5)
    with pytest.raises(ValidationError, match="HARD_CAP"):
        _settings(agent_memory_hard_cap_turns=4)
    with pytest.raises(ValidationError):
        _settings(agent_orch_max_specialist_calls=0)


def test_foundry_needs_a_base_url_and_dimension_is_fixed() -> None:
    with pytest.raises(ValidationError, match="AGENT_LLM_BASE_URL"):
        _settings(agent_llm_provider="foundry")
    with pytest.raises(ValidationError, match="KB_EMBEDDING_DIM"):
        _settings(kb_embedding_dim=768)


def test_dev_user_roles() -> None:
    s = _settings(agent_dev_user_roles="priya.store-manager=store_manager, ravi = store_staff")
    assert s.role_of("priya.store-manager") is AgentRole.STORE_MANAGER
    assert s.role_of("ravi") is AgentRole.STORE_STAFF
    assert s.role_of("someone.else") is AgentRole.STORE_STAFF
    assert parse_user_roles("") == {}
    with pytest.raises(ValidationError, match="user=role"):
        _settings(agent_dev_user_roles="priya")
    with pytest.raises(ValidationError, match="it_support"):
        _settings(agent_dev_user_roles="priya=it_support")


def test_envelope() -> None:
    now = datetime(2026, 11, 9, 9, 0, tzinfo=UTC)
    ok = success({"findings": []}, now, ["data.findings"])
    assert ok.ok and ok.error is None and ok.untrusted_fields == ["data.findings"]
    bad = failure("timeout", "the query took too long")
    assert not bad.ok and bad.error.code is ErrorCode.TIMEOUT and bad.error.retryable
    assert not failure(ErrorCode.FORBIDDEN, "no").error.retryable
    with pytest.raises(ValueError):
        failure("teapot", "unknown code")
    with pytest.raises(ValueError):
        success({}, datetime(2026, 11, 9, 9, 0))


def test_node_ref_checks_ids() -> None:
    ref = NodeRef(
        kind=NodeKind.SERVICE,
        id="payment_gateway",
        name="Payments",
        element_type="payment_service",
        label="Card payment service",
    )
    assert ref.id == "payment_gateway"
    with pytest.raises(ValidationError):
        NodeRef(kind=NodeKind.DEVICE, id="PRN-02", name="PRN-02", element_type="x", label="x")
