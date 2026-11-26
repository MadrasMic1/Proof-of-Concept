# app/agentic/common/tokens.py
"""The User Context Token: who an MCP tool call is for.

Minted by the chat gateway (Agentic Phase 9; for now by tests and scripts.mcp_call)
for ONE specialist run, sent as "Authorization: Bearer <token>" and verified by the
MCP server on EVERY call. The model never sees it. Tools have no store / user / role
parameters: those come only from here.

HS256 with AGENT_USER_CONTEXT_TOKEN_SECRET, issuer / audience from settings,
lifetime AGENT_USER_CONTEXT_TOKEN_TTL_SECONDS, clock skew AGENT_TOKEN_CLOCK_SKEW_SECONDS.
Expiry is checked against the real clock (PyJWT), never a test clock.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.agentic.config import AgenticSettings
from app.agentic.values import AgentRole, SpecialistName

ALGORITHM = "HS256"
REQUIRED_CLAIMS = ("exp", "iat", "iss", "aud", "sub", "jti")


class TokenError(ValueError):
    """The token is missing, malformed, expired, forged or has bad claims."""


@dataclass(frozen=True)
class ToolIdentity:
    user_id: str
    role: AgentRole
    store_id: uuid.UUID
    agent: SpecialistName
    correlation_id: uuid.UUID
    policy_version: str
    chat_session_id: uuid.UUID | None = None
    token_id: str = ""


def _secret(settings: AgenticSettings) -> str:
    secret = settings.agent_user_context_token_secret
    if secret is None or not secret.get_secret_value().strip():
        raise TokenError("AGENT_USER_CONTEXT_TOKEN_SECRET is not set")
    return secret.get_secret_value()


def mint_token(
    settings: AgenticSettings,
    *,
    user_id: str,
    role: AgentRole | str,
    store_id: uuid.UUID,
    agent: SpecialistName | str,
    correlation_id: uuid.UUID,
    policy_version: str,
    chat_session_id: uuid.UUID | None = None,
    issued_at: datetime | None = None,
    ttl_seconds: int | None = None,
) -> str:
    issued = issued_at or datetime.now(UTC)
    ttl = settings.agent_user_context_token_ttl_seconds if ttl_seconds is None else ttl_seconds
    claims = {
        "iss": settings.agent_user_context_token_issuer,
        "aud": settings.agent_user_context_token_audience,
        "sub": user_id,
        "role": str(AgentRole(role)),
        "store_id": str(store_id),
        "agent": str(SpecialistName(agent)),
        "correlation_id": str(correlation_id),
        "policy_version": str(policy_version),
        "chat_session_id": str(chat_session_id) if chat_session_id else None,
        "iat": issued,
        "exp": issued + timedelta(seconds=ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _secret(settings), algorithm=ALGORITHM)


def _uuid(claims: dict[str, object], name: str, required: bool = True) -> uuid.UUID | None:
    value = claims.get(name)
    if value in (None, "") and not required:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError as exc:
        raise TokenError(f"claim {name} must be a uuid") from exc


def verify_token(settings: AgenticSettings, token: str | None) -> ToolIdentity:
    if not token or not token.strip():
        raise TokenError("no bearer token")
    try:
        claims = jwt.decode(
            token.strip(),
            _secret(settings),
            algorithms=[ALGORITHM],
            audience=settings.agent_user_context_token_audience,
            issuer=settings.agent_user_context_token_issuer,
            leeway=settings.agent_token_clock_skew_seconds,
            options={"require": list(REQUIRED_CLAIMS)},
        )
    except jwt.PyJWTError as exc:
        raise TokenError(f"token rejected: {exc}") from exc
    try:
        role = AgentRole(str(claims.get("role")))
        agent = SpecialistName(str(claims.get("agent")))
    except ValueError as exc:
        raise TokenError("unknown role or agent in token") from exc
    user = str(claims.get("sub") or "").strip()
    if not user:
        raise TokenError("claim sub is empty")
    store_id = _uuid(claims, "store_id")
    correlation_id = _uuid(claims, "correlation_id")
    assert store_id is not None and correlation_id is not None
    return ToolIdentity(
        user_id=user,
        role=role,
        store_id=store_id,
        agent=agent,
        correlation_id=correlation_id,
        policy_version=str(claims.get("policy_version") or ""),
        chat_session_id=_uuid(claims, "chat_session_id", required=False),
        token_id=str(claims.get("jti")),
    )
