# app/agentic/tools/itsm.py
"""ITSM tools: read incidents (Store Insight, Incident Desk) and prepare / revise /
read incident drafts (Incident Desk, store managers only; Agentic Phase 4). No tool
creates an incident: that is the Confirm tap in the gateway (Agentic Phase 9)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.drafts.duplicates import check_duplicates
from app.agentic.drafts.service import (
    DraftContext,
    DraftError,
    DraftProposal,
    DraftRevision,
    get_draft,
    prepare_draft,
    revise_draft,
)
from app.agentic.drafts.triage import overlap
from app.agentic.store_context.queries import database_now
from app.agentic.tools.base import (
    Args,
    ToolContext,
    ToolFailure,
    ToolOutput,
    load_store,
    parse_uuid,
)
from app.agentic.values import AgentRole, ErrorCode
from app.queries.incidents import incident_detail
from app.services.incident_rules import state_label

INCIDENT_NUMBER = r"^INC[0-9]{8}$"
DRAFT_UNTRUSTED = ["draft.u_symptom"]


class SearchArgs(Args):
    state: Literal["active", "recent_resolved", "all"] = "active"
    node_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=200)
    limit: int | None = Field(default=None, ge=1, le=50)


class IncidentArgs(Args):
    number: str = Field(pattern=INCIDENT_NUMBER)


class DuplicateArgs(Args):
    finding_id: str | None = Field(default=None, max_length=40)
    text: str = Field(min_length=1, max_length=300)


class PrepareArgs(Args):
    short_description: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=6000)
    u_symptom: str | None = Field(default=None, max_length=500)
    subcategory: str | None = Field(default=None, max_length=60)
    category: Literal["network", "hardware", "software", "inquiry"] | None = None
    impact: int = Field(ge=1, le=3)
    urgency: int = Field(ge=1, le=3)
    reasons: dict[str, str] = Field(default_factory=dict, max_length=6)
    finding_id: str | None = Field(default=None, max_length=40)
    guide_id: str | None = Field(default=None, max_length=64)
    acknowledge_possible_duplicates: bool = False


class ReviseArgs(Args):
    draft_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    short_description: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=6000)
    subcategory: str | None = Field(default=None, max_length=60)
    impact: int | None = Field(default=None, ge=1, le=3)
    urgency: int | None = Field(default=None, ge=1, le=3)
    reasons: dict[str, str] = Field(default_factory=dict, max_length=6)


class DraftArgs(Args):
    draft_id: str = Field(min_length=1, max_length=64)


def _draft_ctx(ctx: ToolContext) -> DraftContext:
    return DraftContext(
        ctx.store_id, ctx.identity.user_id, ctx.chat_session_id(), AgentRole(ctx.identity.role)
    )


def _draft_failure(error: DraftError) -> ToolFailure:
    extra = f" {error.details}" if error.details else ""
    return ToolFailure(error.code, f"{error.message}{extra}")


async def itsm_search_incidents(
    session: AsyncSession, ctx: ToolContext, args: SearchArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    snapshot = loaded.snapshot
    pool = []
    if args.state in ("active", "all"):
        pool += snapshot.incidents
    if args.state in ("recent_resolved", "all"):
        pool += snapshot.recently_resolved
    if args.node_id:
        pool = [i for i in pool if i.affected is not None and i.affected.id == args.node_id]
    if args.text and args.text.strip():
        pool = [i for i in pool if overlap(args.text, i.short_description) > 0]
    most = ctx.settings.mcp_max_incident_results
    limit = min(args.limit or most, most)
    return ToolOutput(
        {
            "incidents": [i.model_dump(mode="json") for i in pool[:limit]],
            "more": max(len(pool) - limit, 0),
        },
        snapshot.as_of,
        ["incidents[].short_description", "incidents[].eta_note"],
    )


async def itsm_get_incident(
    session: AsyncSession, ctx: ToolContext, args: IncidentArgs
) -> ToolOutput:
    found = await incident_detail(session, ctx.store_id, args.number)
    if found is None:
        raise ToolFailure(ErrorCode.NOT_FOUND, f"incident {args.number} not found in this store")
    row, comments = found
    updates = [
        {"at": c.created_at.isoformat(), "author_type": c.author_type, "text": c.value}
        for c in comments[: ctx.settings.mcp_max_public_updates]
    ]
    return ToolOutput(
        {
            "number": row.number,
            "short_description": row.short_description,
            "description": row.description,
            "u_symptom": row.u_symptom,
            "state": row.state,
            "state_label": state_label(row.state),
            "hold_reason": row.hold_reason,
            "priority": row.priority,
            "assignment_group": row.group_name,
            "assigned_to": row.assigned_to,
            "opened_at": row.opened_at.isoformat(),
            "eta": row.u_resolution_eta.isoformat() if row.u_resolution_eta else None,
            "eta_note": row.u_eta_note,
            "affected_type": row.ci_type,
            "affected_kind": row.ci_kind,
            "latest_it_comment": row.latest_it_comment,
            "public_updates": updates,
        },
        await database_now(session),
        [
            "short_description",
            "description",
            "u_symptom",
            "eta_note",
            "latest_it_comment",
            "public_updates[].text",
        ],
    )


async def itsm_check_duplicates(
    session: AsyncSession, ctx: ToolContext, args: DuplicateArgs
) -> ToolOutput:
    loaded = await load_store(session, ctx)
    finding = None
    if args.finding_id:
        finding = next(
            (f for f in loaded.snapshot.findings if f.finding_id == args.finding_id), None
        )
        if finding is None:
            raise ToolFailure(ErrorCode.CONFLICT, "that problem is no longer reported")
    check = await check_duplicates(
        session,
        loaded.snapshot,
        store_id=ctx.store_id,
        chat_session_id=ctx.chat_session_id(),
        finding=finding,
        subject=None,
        text=args.text,
        now=ctx.now,
        lookback_hours=ctx.settings.incident_dup_lookback_hours,
        min_score=ctx.settings.incident_dup_text_min_score,
    )
    return ToolOutput(check.as_json(), loaded.snapshot.as_of, ["matches[].short_description"])


async def itsm_prepare_draft(
    session: AsyncSession, ctx: ToolContext, args: PrepareArgs
) -> ToolOutput:
    proposal = DraftProposal(
        short_description=args.short_description,
        description=args.description,
        u_symptom=args.u_symptom,
        subcategory=args.subcategory,
        category=args.category,
        impact=args.impact,
        urgency=args.urgency,
        reasons=args.reasons,
        finding_id=args.finding_id,
        guide_id=parse_uuid(args.guide_id, "guide_id") if args.guide_id else None,
        acknowledge_possible_duplicates=args.acknowledge_possible_duplicates,
    )
    try:
        result = await prepare_draft(
            session, _draft_ctx(ctx), proposal, now=ctx.now, catalogue=ctx.catalogue,
            settings=ctx.settings,
        )  # fmt: skip
    except DraftError as exc:
        raise _draft_failure(exc) from exc
    return ToolOutput(
        {
            "outcome": result.outcome,
            "duplicates": result.duplicates.as_json(),
            "draft": result.draft.model_dump(mode="json") if result.draft else None,
            "warnings": result.warnings,
        },
        await database_now(session),
        [*DRAFT_UNTRUSTED, "duplicates.matches[].short_description"],
    )


async def itsm_revise_draft(
    session: AsyncSession, ctx: ToolContext, args: ReviseArgs
) -> ToolOutput:
    revision = DraftRevision(
        version=args.version,
        short_description=args.short_description,
        description=args.description,
        subcategory=args.subcategory,
        impact=args.impact,
        urgency=args.urgency,
        reasons=args.reasons,
    )
    try:
        view = await revise_draft(
            session, _draft_ctx(ctx), parse_uuid(args.draft_id, "draft_id"), revision,
            now=ctx.now, catalogue=ctx.catalogue, settings=ctx.settings,
        )  # fmt: skip
    except DraftError as exc:
        raise _draft_failure(exc) from exc
    return ToolOutput(
        {"draft": view.model_dump(mode="json")}, await database_now(session), DRAFT_UNTRUSTED
    )


async def itsm_get_draft(session: AsyncSession, ctx: ToolContext, args: DraftArgs) -> ToolOutput:
    try:
        view = await get_draft(
            session, _draft_ctx(ctx), parse_uuid(args.draft_id, "draft_id"), now=ctx.now
        )
    except DraftError as exc:
        raise _draft_failure(exc) from exc
    return ToolOutput(
        {"draft": view.model_dump(mode="json")}, await database_now(session), DRAFT_UNTRUSTED
    )
