# app/agentic/values.py
"""Allowed values of the agentic tables (migration 0005) and agentic constants.

Kept apart from app/models/values.py so the existing value lists stay untouched.
tests/agentic/test_agentic_values_match_db.py checks every enum against the
database CHECK constraint, exactly like tests/test_values_match_db.py.
"""

from __future__ import annotations

from enum import StrEnum

# Fixed in migration 0005 (agent_kb_chunks.embedding vector(1536)). Changing it
# needs a new migration and a full KB re-ingest.
EMBEDDING_DIM = 1536

# Database logins of the agentic processes (created by scripts.agentic_db_setup).
MCP_ROLE = "ric_mcp"
KB_ADMIN_ROLE = "ric_kb_admin"


# --- Users and conversations ----------------------------------------------------------


class AgentRole(StrEnum):
    """Who is chatting. Only store managers draft, confirm, edit or cancel incidents."""

    STORE_STAFF = "store_staff"
    STORE_MANAGER = "store_manager"


class ChatSessionStatus(StrEnum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CLOSED = "closed"


class ConversationState(StrEnum):
    """Derived by the gateway from the guide and draft rows; never written by a model."""

    IDLE = "idle"
    GUIDING = "guiding"
    GUIDE_RESOLVED = "guide_resolved"
    GUIDE_EXHAUSTED = "guide_exhausted"
    DRAFT_PENDING = "draft_pending"
    INCIDENT_RAISED = "incident_raised"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


# --- Store graph ----------------------------------------------------------------------


class DependencySourceKind(StrEnum):
    DEVICE = "device"
    NETWORK_ELEMENT = "network_element"


class DependencyTargetKind(StrEnum):
    DEVICE = "device"
    NETWORK_ELEMENT = "network_element"
    SERVICE = "service"


class DependencyRelation(StrEnum):
    CONNECTS_VIA = "connects_via"
    POWERED_BY = "powered_by"
    USES_SERVICE = "uses_service"


# --- Knowledge base -------------------------------------------------------------------


class KbArticleStatus(StrEnum):
    PUBLISHED = "published"
    RETIRED = "retired"


class KbAudience(StrEnum):
    STORE_STAFF = "store_staff"
    TECHNICIAN = "technician"


class KbChunkType(StrEnum):
    ARTICLE = "article"
    STEP = "step"


class GuideStatus(StrEnum):
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    EXHAUSTED = "exhausted"
    NEEDS_TECHNICIAN = "needs_technician"
    ABANDONED = "abandoned"


# --- Incident drafts ------------------------------------------------------------------


class DraftStatus(StrEnum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    FAILED = "failed"


# --- Agents, LLM calls and tool calls -------------------------------------------------


class AgentName(StrEnum):
    """Everything that calls a model (agent_llm_calls.agent)."""

    ORCHESTRATOR = "orchestrator"
    STORE_INSIGHT = "store_insight"
    KB_GUIDE = "kb_guide"
    INCIDENT_DESK = "incident_desk"
    SUMMARISER = "summariser"


class SpecialistName(StrEnum):
    """The agents that call MCP tools (agent_tool_calls.agent, the token's agent claim)."""

    STORE_INSIGHT = "store_insight"
    KB_GUIDE = "kb_guide"
    INCIDENT_DESK = "incident_desk"


class LlmProfile(StrEnum):
    ORCHESTRATOR = "orchestrator"
    SPECIALIST = "specialist"
    SUMMARISER = "summariser"


class LlmCallStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REFUSED = "refused"  # kill switch or budget: no request was sent


class ErrorCode(StrEnum):
    """Error codes of the MCP tool result envelope."""

    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    RATE_LIMITED = "rate_limited"
    INVALID_TOKEN = "invalid_token"
    POLICY_MISMATCH = "policy_mismatch"
    ERROR = "error"


RETRYABLE_ERRORS: frozenset[ErrorCode] = frozenset(
    {ErrorCode.TIMEOUT, ErrorCode.UNAVAILABLE, ErrorCode.RATE_LIMITED}
)


class ToolOutcome(StrEnum):
    """agent_tool_calls.outcome: 'ok' or one of the error codes."""

    OK = "ok"
    NOT_FOUND = "not_found"
    FORBIDDEN = "forbidden"
    INVALID_INPUT = "invalid_input"
    CONFLICT = "conflict"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    RATE_LIMITED = "rate_limited"
    INVALID_TOKEN = "invalid_token"
    POLICY_MISMATCH = "policy_mismatch"
    ERROR = "error"
