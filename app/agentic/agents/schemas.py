# app/agentic/agents/schemas.py
"""Briefs (orchestrator -> specialist), trusted context (gateway), the reports the
models submit (submit_result) and the result every specialist returns.

The model submits a SMALL report (an outcome, a summary and ids). Code builds the
result body from the tool outputs of the same run, so step text, draft fields,
staff_can_fix, kb_tags and every id come from data, never from model text.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SuggestedNext = Literal[
    "none", "guide", "offer_ticket", "show_draft", "check_incident", "rediagnose", "ask_device"
]


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


@dataclass(frozen=True)
class TrustedContext:
    """Set by the gateway (Agentic Phase 8); scripts and tests build it until then."""

    user_id: str
    role: str
    store_id: uuid.UUID
    correlation_id: uuid.UUID
    chat_session_id: uuid.UUID | None = None
    guide_id: uuid.UUID | None = None  # the guide in progress (continue)
    draft_id: uuid.UUID | None = None  # the pending draft (revise)


# --- briefs -------------------------------------------------------------------------


class StoreInsightBrief(_Model):
    question: str = Field(min_length=1, max_length=500)
    devices_mentioned: list[str] = Field(default_factory=list, max_length=5)
    user_reported: list[str] = Field(default_factory=list, max_length=5)


class KbGuideBrief(_Model):
    mode: Literal["start", "continue"]
    issue: str | None = Field(default=None, max_length=500)
    element_type: str | None = None
    fault_code: str | None = None
    kb_tags: list[str] = Field(default_factory=list, max_length=10)
    subject_node_id: str | None = None
    finding_ids: list[str] = Field(default_factory=list, max_length=5)
    user_report: str | None = Field(default=None, max_length=500)


class IncidentDeskBrief(_Model):
    mode: Literal["lookup", "draft", "revise"]
    issue: str = Field(min_length=1, max_length=500)
    user_words: str | None = Field(default=None, max_length=500)
    finding_id: str | None = None
    revision_request: str | None = Field(default=None, max_length=500)
    acknowledge_possible_duplicates: bool = False


# --- reports (the submit_result tool's parameters) -----------------------------------


class StoreInsightReport(_Model):
    """Store Insight's answer. Ids only from this run's tool results."""

    outcome: Literal[
        "cause_found",
        "covered_by_incident",
        "planned_maintenance",
        "no_fault_found",
        "status_unknown",
        "ambiguous_device",
    ] = Field(description="what the diagnosis found")
    summary: str = Field(max_length=400, description="1-3 plain sentences for the store")
    finding_ids: list[str] = Field(
        default_factory=list, description="finding ids (from store_get_findings) that explain it"
    )
    subject_node_ids: list[str] = Field(
        default_factory=list, description="node ids of the devices the user asked about"
    )
    suspected_node_ids: list[str] = Field(
        default_factory=list, description="node ids suspected without a finding (exploration)"
    )
    device_option_ids: list[str] = Field(
        default_factory=list, description="candidate node ids when the device is ambiguous"
    )


class KbGuideReport(_Model):
    outcome: Literal[
        "needs_user_input",
        "resolved",
        "exhausted",
        "needs_technician",
        "no_article",
        "choose_article",
        "problem_changed",
    ]
    summary: str = Field(max_length=400, description="1-2 plain sentences")
    guide_id: str | None = Field(default=None, description="the guide id from the tools")
    candidate_kb_refs: list[str] = Field(
        default_factory=list, description="kb_refs to offer when choosing an article"
    )


class IncidentDeskReport(_Model):
    outcome: Literal[
        "incidents_found",
        "no_incidents",
        "duplicate_found",
        "possible_duplicate",
        "draft_ready",
        "draft_revised",
        "needs_rediagnosis",
    ]
    summary: str = Field(max_length=400, description="1-2 plain sentences")
    incident_numbers: list[str] = Field(
        default_factory=list, description="INC numbers from the tool results that matter"
    )
    draft_id: str | None = Field(default=None, description="the draft id from the tools")


# --- result ---------------------------------------------------------------------------


class ToolCallTrace(_Model):
    tool: str
    arguments: dict[str, Any]
    ok: bool
    error_code: str | None = None


class RunTrace(_Model):
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)
    model_calls: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 1
    fallback_used: bool = False
    model: str = ""
    stop_reason: str | None = None


class SpecialistResult(_Model):
    agent: str
    status: Literal["ok", "partial", "not_found", "error"]
    outcome: str
    summary: str
    suggested_next: SuggestedNext = "none"
    body: dict[str, Any] = Field(default_factory=dict)
    problems: list[str] = Field(default_factory=list)
    trace: RunTrace = Field(default_factory=RunTrace)
