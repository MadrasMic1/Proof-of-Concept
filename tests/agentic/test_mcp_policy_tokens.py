# tests/agentic/test_mcp_policy_tokens.py
"""Agentic Phase 5: tokens, tool policy, pipeline helpers, import boundary (no database)."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest

from app.agentic.common.tokens import TokenError, mint_token, verify_token
from app.agentic.config import AgenticSettings
from app.agentic.tools.pipeline import RateLimiter, args_hash, cap_untrusted
from app.agentic.tools.policy import PolicyError, load_policy
from app.agentic.tools.registry import TOOLS
from app.agentic.values import AgentRole, SpecialistName

ROOT = Path(__file__).resolve().parents[2]
POLICY = load_policy(ROOT / "config" / "tool_policy.yaml")
SECRET = "t" * 40
STORE = uuid.uuid4()


def _settings(**values: object) -> AgenticSettings:
    values.setdefault("agent_user_context_token_secret", SECRET)
    return AgenticSettings(_env_file=None, environment="test", **values)


def _token(settings: AgenticSettings | None = None, **values: object) -> str:
    claims: dict[str, object] = {
        "user_id": "priya.store-manager",
        "role": "store_manager",
        "store_id": STORE,
        "agent": "incident_desk",
        "correlation_id": uuid.uuid4(),
        "policy_version": "1",
        **values,
    }
    return mint_token(settings or _settings(), **claims)  # type: ignore[arg-type]


def test_token_round_trip() -> None:
    chat = uuid.uuid4()
    identity = verify_token(_settings(), _token(chat_session_id=chat))
    assert identity.user_id == "priya.store-manager"
    assert identity.role is AgentRole.STORE_MANAGER
    assert identity.agent is SpecialistName.INCIDENT_DESK
    assert (identity.store_id, identity.chat_session_id) == (STORE, chat)
    assert identity.policy_version == "1" and identity.token_id
    assert verify_token(_settings(), _token()).chat_session_id is None


@pytest.mark.parametrize(
    ("token", "message"),
    [
        (None, "no bearer token"),
        ("not-a-jwt", "token rejected"),
        (_token(_settings(agent_user_context_token_secret="o" * 40)), "Signature"),
        (_token(_settings(agent_user_context_token_audience="someone-else")), "(?i)audience"),
        (_token(issued_at=datetime.now(UTC) - timedelta(hours=2), ttl_seconds=60), "expired"),
    ],
)
def test_bad_tokens(token: str | None, message: str) -> None:
    with pytest.raises(TokenError, match=message):
        verify_token(_settings(), token)


def test_unknown_role_is_refused() -> None:
    now = datetime.now(UTC)
    forged = jwt.encode(
        {
            "iss": "ric-gateway",
            "aud": "ric-mcp",
            "sub": "x",
            "role": "it_support",
            "agent": "store_insight",
            "store_id": str(STORE),
            "correlation_id": str(uuid.uuid4()),
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "1",
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="unknown role or agent"):
        verify_token(_settings(), forged)


def test_policy_matches_the_tools_and_denies_by_default() -> None:
    assert POLICY.problems_against(TOOLS) == []
    assert len(TOOLS) == 19
    assert POLICY.allows("kb_guide", "store_staff", "kb_guide_start")
    assert POLICY.allows("incident_desk", "store_manager", "itsm_prepare_draft")
    assert not POLICY.allows("incident_desk", "store_staff", "itsm_prepare_draft")
    assert not POLICY.allows("store_insight", "store_manager", "kb_find_articles")
    assert not POLICY.allows("store_insight", "store_manager", "unknown_tool")
    assert POLICY.tools_for("incident_desk", "store_staff") == [
        "itsm_search_incidents",
        "itsm_get_incident",
    ]
    assert POLICY.problems_against([*TOOLS, "extra_tool"]) == [
        "tool 'extra_tool' is not in the policy"
    ]


def test_bad_policy_file(tmp_path: Path) -> None:
    with pytest.raises(PolicyError, match="not found"):
        load_policy(tmp_path / "missing.yaml")
    bad = tmp_path / "policy.yaml"
    bad.write_text("version: 1\nroles: [store_staff]\nagents: {}\n", encoding="utf-8")
    with pytest.raises(PolicyError, match="roles must be exactly"):
        load_policy(bad)


def test_tool_names_are_function_calling_safe() -> None:
    for name, spec in TOOLS.items():
        assert name.replace("_", "").isalnum() and len(name) <= 64
        lines = spec.description.splitlines()
        assert lines[0].startswith("Use when:"), name
        assert any(line.startswith("Returns:") for line in lines), name


def test_cap_untrusted_and_hash() -> None:
    data = {"items": [{"text": "x" * 20}, {"text": "short"}], "title": "y" * 20, "n": 1}
    for path in ("items[].text", "title", "missing[].x", "n"):
        data = cap_untrusted(data, path, 10)
    assert data["items"][0]["text"] == "x" * 10 + " [truncated]"
    assert data["items"][1]["text"] == "short" and data["title"].endswith("[truncated]")
    assert args_hash({"a": 1, "b": 2}) == args_hash({"b": 2, "a": 1})


def test_rate_limiter() -> None:
    limiter = RateLimiter(per_minute=3, identical_max=2)
    assert limiter.check("chat", "same", 0.0) is None
    assert limiter.check("chat", "same", 1.0) is None
    assert "same call" in (limiter.check("chat", "same", 2.0) or "")
    assert limiter.check("chat", "other", 3.0) is None
    assert "tool calls in a minute" in (limiter.check("chat", "third", 4.0) or "")
    assert limiter.check("chat", "fourth", 70.0) is None


def test_mcp_server_does_not_import_the_api() -> None:
    code = (
        "import sys, mcp_server.app, mcp_server.runtime; "
        "print([m for m in sys.modules if m == 'app.main' or m.startswith('app.api')])"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "[]"
