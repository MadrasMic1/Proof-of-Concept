# app/agentic/drafts/service.py
"""Incident drafts: prepare, revise, read, cancel. Never creates an incident.

prepare_draft, in order:
    1. the caller is a store manager; the chat session is theirs, in this store
    2. rate limit: MCP_MAX_DRAFTS_PER_SESSION_PER_HOUR drafts per chat session
    3. a FRESH snapshot (Phase 2): the finding is looked up by its stable id; if the
       problem has cleared, the draft is refused (conflict)
    4. the guide (optional) must be this chat's, ended exhausted / needs_technician
    5. duplicate check: strong -> no draft; possible -> no draft unless acknowledged
    6. baseline (triage.py); the model's impact / urgency are clamped to
       INCIDENT_*_MAX_DEVIATION levels of it; category from the catalogue
    7. the model's text: length limits, every device / element name and incident
       number it mentions must be in the evidence, then personal data masked
    8. the generated evidence block is appended; the chat's other pending draft is
       superseded; the new draft is pending for INCIDENT_DRAFT_TTL_MINUTES
Runs in the caller's transaction and never commits (the MCP process, Agentic
Phase 6, calls it with the ric_mcp login: insert / update on drafts only).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.common.types import Finding, NodeKind, NodeRef
from app.agentic.config import AgenticSettings
from app.agentic.drafts import triage
from app.agentic.drafts.duplicates import DuplicateCheck, check_duplicates, numbers
from app.agentic.kb.articles import Article
from app.agentic.kb.guide import build_view
from app.agentic.store_context.assemble import StoreSnapshot, assemble_snapshot
from app.agentic.store_context.snapshot import read_store_state
from app.agentic.store_graph.catalogue import Catalogue
from app.agentic.values import AgentRole, DraftStatus, ErrorCode, GuideStatus
from app.models import (
    AgentChatSession,
    AgentIncidentDraft,
    AgentKbArticle,
    AgentKbGuideSession,
    AssignmentGroup,
)
from app.models.values import IncidentCategory

STORE_LEVEL_GROUP = "Service Desk"
HANDOFF_GUIDE_STATES = (GuideStatus.EXHAUSTED, GuideStatus.NEEDS_TECHNICIAN)
EVIDENCE_HEADER = "--- RIC evidence (generated) ---"
SUBCATEGORY_MAX_CHARS = 60
SYMPTOM_MAX_CHARS = 500


class DraftError(Exception):
    def __init__(
        self, code: ErrorCode, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class DraftContext:
    """Who is asking. From the gateway / the signed token, never from a model."""

    store_id: uuid.UUID
    user_id: str
    chat_session_id: uuid.UUID
    role: AgentRole


class DraftProposal(BaseModel):
    """What the Incident Desk agent proposes (the wrapper adds finding / guide ids)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    short_description: str
    description: str
    u_symptom: str | None = None
    subcategory: str | None = None
    category: str | None = None  # only used when there is no affected CI
    impact: int = Field(ge=1, le=3)
    urgency: int = Field(ge=1, le=3)
    reasons: dict[str, str] = Field(default_factory=dict)
    finding_id: str | None = None
    guide_id: uuid.UUID | None = None
    acknowledge_possible_duplicates: bool = False


class DraftRevision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int
    short_description: str | None = None
    description: str | None = None
    subcategory: str | None = None
    impact: int | None = Field(default=None, ge=1, le=3)
    urgency: int | None = Field(default=None, ge=1, le=3)
    reasons: dict[str, str] = Field(default_factory=dict)


class DraftView(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    version: int
    status: DraftStatus
    short_description: str
    description: str
    u_symptom: str | None
    category: str
    subcategory: str | None
    impact: int
    urgency: int
    priority: int
    baseline_impact: int
    baseline_urgency: int
    reasons: dict[str, Any]
    assignment_group: str | None
    affected: NodeRef | None
    impacted: list[str]
    finding_ids: list[str]
    steps_tried: list[dict[str, Any]]
    duplicate_check: dict[str, Any]
    expires_at: datetime


@dataclass
class PrepareResult:
    outcome: Literal["draft_ready", "duplicate_found", "possible_duplicate"]
    duplicates: DuplicateCheck
    draft: DraftView | None = None
    warnings: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------------


def _limits(text: str, limit: int, label: str) -> str:
    value = " ".join(text.split()) if label == "short_description" else text.strip()
    if not value:
        raise DraftError(ErrorCode.INVALID_INPUT, f"{label} is required")
    if len(value) > limit:
        raise DraftError(ErrorCode.INVALID_INPUT, f"{label} is longer than {limit} characters")
    return value


def _view(row: AgentIncidentDraft, group_name: str | None) -> DraftView:
    evidence = row.evidence or {}
    affected = evidence.get("affected")
    return DraftView(
        draft_id=str(row.id),
        version=row.version,
        status=DraftStatus(row.status),
        short_description=row.short_description,
        description=row.description,
        u_symptom=row.u_symptom,
        category=row.category,
        subcategory=row.subcategory,
        impact=row.impact,
        urgency=row.urgency,
        priority=row.impact + row.urgency - 1,
        baseline_impact=row.baseline_impact,
        baseline_urgency=row.baseline_urgency,
        reasons=dict(row.reasons or {}),
        assignment_group=group_name,
        affected=NodeRef.model_validate(affected) if affected else None,
        impacted=[n["name"] for n in (row.impacted_nodes or [])],
        finding_ids=list(row.finding_ids or []),
        steps_tried=list(row.steps_tried or []),
        duplicate_check=dict(row.duplicate_check or {}),
        expires_at=row.expires_at,
    )


async def _group_name(session: AsyncSession, group_id: uuid.UUID | None) -> str | None:
    if group_id is None:
        return None
    return await session.scalar(select(AssignmentGroup.name).where(AssignmentGroup.id == group_id))


async def _check_chat(session: AsyncSession, ctx: DraftContext) -> None:
    found = await session.scalar(
        select(AgentChatSession.id).where(
            AgentChatSession.id == ctx.chat_session_id,
            AgentChatSession.store_id == ctx.store_id,
            AgentChatSession.user_id == ctx.user_id,
        )
    )
    if found is None:
        raise DraftError(ErrorCode.NOT_FOUND, "chat session not found")


def _require_manager(ctx: DraftContext) -> None:
    if ctx.role != AgentRole.STORE_MANAGER:
        raise DraftError(ErrorCode.FORBIDDEN, "only a store manager can raise incidents")


def _local(value: datetime | None, tz: ZoneInfo) -> str:
    return value.astimezone(tz).strftime("%d %b %H:%M") if value else "?"


def evidence_block(
    snapshot: StoreSnapshot,
    finding: Finding | None,
    subject: NodeRef | None,
    steps: list[dict[str, Any]],
    guide_ref: str | None,
    draft_id: uuid.UUID,
) -> str:
    tz = ZoneInfo(snapshot.store.timezone)
    zone = snapshot.store.local_time.tzname() or snapshot.store.timezone
    lines = [EVIDENCE_HEADER]
    if finding is not None:
        since = finding.evidence_path[0].last_report_at if finding.evidence_path else None
        fault = f" ({finding.fault_code})" if finding.fault_code else ""
        lines.append(
            f"Diagnosis: {finding.root_node.label} {finding.status.value.upper()}{fault} "
            f"since {_local(since, tz)} {zone}"
        )
        lines.append(f"Impact: {finding.impact_text}")
        if finding.impacted_nodes:
            lines.append("Affected: " + ", ".join(n.name for n in finding.impacted_nodes))
    elif subject is not None:
        lines.append(f"Reported item: {subject.label}")
    else:
        lines.append("Diagnosis: no matching fault reported by monitoring")
    if steps:
        tried = "; ".join(f"{s['step_id']} {s['outcome_label']}" for s in steps)
        lines.append(f"Steps already tried ({guide_ref}): {tried}")
    lines.append(f"Store: {snapshot.store.site_code} {snapshot.store.name}")
    lines.append(f"Source: RIC assistant draft {draft_id}")
    return "\n".join(lines)


def _check_mentions(
    text: str, snapshot: StoreSnapshot, allowed_names: set[str], allowed_numbers: set[str]
) -> None:
    names = {v.node.name for v in snapshot.nodes if v.node.kind != NodeKind.SERVICE}
    unknown_names = sorted(triage.mentioned_names(text, names) - allowed_names)
    unknown_numbers = sorted(triage.incident_numbers(text) - allowed_numbers)
    if unknown_names or unknown_numbers:
        raise DraftError(
            ErrorCode.INVALID_INPUT,
            "the text mentions items that are not in the evidence: "
            + ", ".join([*unknown_names, *unknown_numbers]),
            {"not_in_evidence": [*unknown_names, *unknown_numbers]},
        )


async def _guide_steps(
    session: AsyncSession, ctx: DraftContext, guide_id: uuid.UUID
) -> tuple[AgentKbGuideSession, list[dict[str, Any]], str]:
    row = await session.scalar(
        select(AgentKbGuideSession).where(
            AgentKbGuideSession.id == guide_id,
            AgentKbGuideSession.store_id == ctx.store_id,
            AgentKbGuideSession.user_id == ctx.user_id,
            AgentKbGuideSession.chat_session_id == ctx.chat_session_id,
        )
    )
    if row is None:
        raise DraftError(ErrorCode.NOT_FOUND, f"guide {guide_id} not found")
    if row.status not in HANDOFF_GUIDE_STATES:
        raise DraftError(
            ErrorCode.CONFLICT, f"the guide has not ended in a hand-off ({row.status})"
        )
    content = await session.scalar(
        select(AgentKbArticle.content).where(AgentKbArticle.id == row.article_id)
    )
    view = build_view(row, Article.model_validate(content))
    steps = [t.model_dump() for t in view.steps_tried]
    return row, steps, f"{row.kb_ref} v{row.kb_version}"


def _node(snapshot: StoreSnapshot, node_id: uuid.UUID | None) -> NodeRef | None:
    if node_id is None:
        return None
    view = snapshot.node(str(node_id))
    return view.node if view is not None else None


async def fresh_snapshot(
    session: AsyncSession,
    store_id: uuid.UUID,
    *,
    now: datetime,
    catalogue: Catalogue,
    settings: AgenticSettings,
) -> StoreSnapshot:
    state = await read_store_state(session, store_id, now=now, settings=settings)
    return assemble_snapshot(state, catalogue, now=now, settings=settings)


# --- operations --------------------------------------------------------------------


async def prepare_draft(
    session: AsyncSession,
    ctx: DraftContext,
    proposal: DraftProposal,
    *,
    now: datetime,
    catalogue: Catalogue,
    settings: AgenticSettings,
) -> PrepareResult:
    _require_manager(ctx)
    await _check_chat(session, ctx)
    recent = await session.scalar(
        select(func.count())
        .select_from(AgentIncidentDraft)
        .where(
            AgentIncidentDraft.chat_session_id == ctx.chat_session_id,
            AgentIncidentDraft.created_at >= now - timedelta(hours=1),
        )
    )
    if (recent or 0) >= settings.mcp_max_drafts_per_session_per_hour:
        raise DraftError(ErrorCode.RATE_LIMITED, "too many drafts in this chat in the last hour")

    snapshot = await fresh_snapshot(
        session, ctx.store_id, now=now, catalogue=catalogue, settings=settings
    )
    finding: Finding | None = None
    if proposal.finding_id is not None:
        finding = next((f for f in snapshot.findings if f.finding_id == proposal.finding_id), None)
        if finding is None:
            raise DraftError(
                ErrorCode.CONFLICT,
                "that problem is no longer reported for this store; diagnose again",
            )

    guide_row = None
    steps: list[dict[str, Any]] = []
    guide_ref: str | None = None
    if proposal.guide_id is not None:
        guide_row, steps, guide_ref = await _guide_steps(session, ctx, proposal.guide_id)
    subject = None
    if guide_row is not None:
        subject = _node(snapshot, guide_row.subject_device_id or guide_row.subject_element_id)
    affected = finding.root_node if finding is not None else subject

    short = _limits(
        proposal.short_description, settings.incident_subject_max_chars, "short_description"
    )
    text = _limits(proposal.description, settings.incident_description_max_chars, "description")
    duplicates = await check_duplicates(
        session,
        snapshot,
        store_id=ctx.store_id,
        chat_session_id=ctx.chat_session_id,
        finding=finding,
        subject=subject,
        text=f"{short} {proposal.u_symptom or ''}",
        now=now,
        lookback_hours=settings.incident_dup_lookback_hours,
        min_score=settings.incident_dup_text_min_score,
    )
    if duplicates.result == "strong":
        return PrepareResult("duplicate_found", duplicates)
    if duplicates.result == "possible" and not proposal.acknowledge_possible_duplicates:
        return PrepareResult("possible_duplicate", duplicates)

    entry = catalogue.types.get(affected.element_type) if affected is not None else None
    if entry is not None:
        category = entry.incident_category
        subcategory = proposal.subcategory or entry.subcategories[0]
        if subcategory not in entry.subcategories:
            raise DraftError(
                ErrorCode.INVALID_INPUT,
                f"subcategory must be one of {entry.subcategories}",
            )
        group = entry.assignment_group
    else:
        try:
            category = IncidentCategory(proposal.category or "").value
        except ValueError as exc:
            choices = [c.value for c in IncidentCategory]
            raise DraftError(ErrorCode.INVALID_INPUT, f"category must be one of {choices}") from exc
        subcategory = (proposal.subcategory or "").strip()[:SUBCATEGORY_MAX_CHARS] or None
        group = STORE_LEVEL_GROUP
    group_id = await session.scalar(select(AssignmentGroup.id).where(AssignmentGroup.name == group))

    base = triage.baseline(
        finding,
        revenue_critical=entry.revenue_critical if entry is not None else False,
        store_open=snapshot.store.open_now,
        guide_needs_technician=(
            guide_row is not None and guide_row.status == GuideStatus.NEEDS_TECHNICIAN
        ),
    )
    impact = triage.clamp(proposal.impact, base.impact, settings.incident_impact_max_deviation)
    urgency = triage.clamp(proposal.urgency, base.urgency, settings.incident_urgency_max_deviation)
    reasons: dict[str, Any] = {
        **{k: str(v)[:300] for k, v in proposal.reasons.items()},
        "baseline_impact": base.impact_reason,
        "baseline_urgency": base.urgency_reason,
    }
    if impact.adjusted:
        reasons["impact_adjusted"] = f"proposed {proposal.impact}, limited to {impact.value}"
    if urgency.adjusted:
        reasons["urgency_adjusted"] = f"proposed {proposal.urgency}, limited to {urgency.value}"

    allowed_names: set[str] = set()
    if finding is not None:
        allowed_names |= {finding.root_node.name, *(n.name for n in finding.impacted_nodes)}
        allowed_names |= {s.node.name for s in finding.evidence_path}
    if subject is not None:
        allowed_names.add(subject.name)
    symptom = (proposal.u_symptom or "").strip()[:SYMPTOM_MAX_CHARS] or None
    all_names = {v.node.name for v in snapshot.nodes if v.node.kind != NodeKind.SERVICE}
    allowed_names |= triage.mentioned_names(symptom or "", all_names)
    allowed_numbers = set(numbers(duplicates))
    _check_mentions(f"{short}\n{text}", snapshot, allowed_names, allowed_numbers)

    draft_id = uuid.uuid4()
    block = (
        evidence_block(snapshot, finding, subject, steps, guide_ref, draft_id)
        if settings.incident_append_evidence_block
        else ""
    )
    model_text = triage.mask_text(text) or ""
    description = f"{model_text}\n\n{block}" if block else model_text

    await session.execute(
        update(AgentIncidentDraft)
        .where(
            AgentIncidentDraft.chat_session_id == ctx.chat_session_id,
            AgentIncidentDraft.status == DraftStatus.PENDING,
        )
        .values(status=DraftStatus.SUPERSEDED)
        .execution_options(synchronize_session=False)
    )
    row = AgentIncidentDraft(
        id=draft_id,
        chat_session_id=ctx.chat_session_id,
        store_id=ctx.store_id,
        user_id=ctx.user_id,
        guide_session_id=guide_row.id if guide_row is not None else None,
        status=DraftStatus.PENDING,
        version=1,
        short_description=triage.mask_text(short) or short,
        description=description,
        u_symptom=triage.mask_text(symptom),
        category=category,
        subcategory=subcategory,
        impact=impact.value,
        urgency=urgency.value,
        baseline_impact=base.impact,
        baseline_urgency=base.urgency,
        reasons=reasons,
        assignment_group_id=group_id,
        cmdb_ci_network_element_id=(
            uuid.UUID(affected.id)
            if affected is not None and affected.kind == NodeKind.NETWORK_ELEMENT
            else None
        ),
        cmdb_ci_device_id=(
            uuid.UUID(affected.id)
            if affected is not None and affected.kind == NodeKind.DEVICE
            else None
        ),
        impacted_nodes=[
            n.model_dump(mode="json") for n in (finding.impacted_nodes if finding else [])
        ],
        finding_ids=[finding.finding_id] if finding is not None else [],
        evidence={
            "affected": affected.model_dump(mode="json") if affected is not None else None,
            "allowed_names": sorted(allowed_names),
            "allowed_numbers": sorted(allowed_numbers),
            "block": block,
            "snapshot_as_of": snapshot.as_of.isoformat(),
            "store_open": snapshot.store.open_now,
        },
        steps_tried=steps,
        duplicate_check=duplicates.as_json(),
        expires_at=now + timedelta(minutes=settings.incident_draft_ttl_minutes),
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return PrepareResult(
        "draft_ready", duplicates, _view(row, group), warnings=list(snapshot.warnings)
    )


async def _load(
    session: AsyncSession, ctx: DraftContext, draft_id: uuid.UUID
) -> AgentIncidentDraft:
    row = await session.scalar(
        select(AgentIncidentDraft)
        .where(
            AgentIncidentDraft.id == draft_id,
            AgentIncidentDraft.store_id == ctx.store_id,
            AgentIncidentDraft.user_id == ctx.user_id,
            AgentIncidentDraft.chat_session_id == ctx.chat_session_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise DraftError(ErrorCode.NOT_FOUND, f"draft {draft_id} not found")
    return row


def _expire_if_due(row: AgentIncidentDraft, now: datetime) -> bool:
    if row.status == DraftStatus.PENDING and now >= row.expires_at:
        row.status = DraftStatus.EXPIRED
        row.updated_at = now
        return True
    return False


async def get_draft(
    session: AsyncSession, ctx: DraftContext, draft_id: uuid.UUID, *, now: datetime
) -> DraftView:
    row = await _load(session, ctx, draft_id)
    if _expire_if_due(row, now):
        await session.flush()
    return _view(row, await _group_name(session, row.assignment_group_id))


async def _pending(
    session: AsyncSession, ctx: DraftContext, draft_id: uuid.UUID, now: datetime
) -> AgentIncidentDraft:
    _require_manager(ctx)
    row = await _load(session, ctx, draft_id)
    if _expire_if_due(row, now):
        await session.flush()
    if row.status != DraftStatus.PENDING:
        raise DraftError(ErrorCode.CONFLICT, f"the draft is {row.status}")
    return row


async def revise_draft(
    session: AsyncSession,
    ctx: DraftContext,
    draft_id: uuid.UUID,
    revision: DraftRevision,
    *,
    now: datetime,
    catalogue: Catalogue,
    settings: AgenticSettings,
) -> DraftView:
    """Text, subcategory, impact and urgency only; the affected item never changes
    (a different device needs a new diagnosis and a new draft)."""
    row = await _pending(session, ctx, draft_id, now)
    if revision.version != row.version:
        raise DraftError(ErrorCode.CONFLICT, f"the draft is at version {row.version}")
    evidence = dict(row.evidence or {})
    snapshot = await fresh_snapshot(
        session, ctx.store_id, now=now, catalogue=catalogue, settings=settings
    )
    allowed_names = set(evidence.get("allowed_names", []))
    allowed_numbers = set(evidence.get("allowed_numbers", []))
    reasons = dict(row.reasons or {})
    if revision.short_description is not None:
        short = _limits(
            revision.short_description, settings.incident_subject_max_chars, "short_description"
        )
        _check_mentions(short, snapshot, allowed_names, allowed_numbers)
        row.short_description = triage.mask_text(short) or short
    if revision.description is not None:
        text = _limits(revision.description, settings.incident_description_max_chars, "description")
        _check_mentions(text, snapshot, allowed_names, allowed_numbers)
        block = evidence.get("block") or ""
        masked = triage.mask_text(text) or ""
        row.description = f"{masked}\n\n{block}" if block else masked
    if revision.subcategory is not None:
        affected = evidence.get("affected")
        entry = catalogue.types.get(affected["element_type"]) if affected else None
        if entry is not None and revision.subcategory not in entry.subcategories:
            raise DraftError(
                ErrorCode.INVALID_INPUT, f"subcategory must be one of {entry.subcategories}"
            )
        row.subcategory = revision.subcategory.strip()[:SUBCATEGORY_MAX_CHARS] or None
    if revision.impact is not None:
        impact = triage.clamp(
            revision.impact, row.baseline_impact, settings.incident_impact_max_deviation
        )
        row.impact = impact.value
        reasons.pop("impact_adjusted", None)
        if impact.adjusted:
            reasons["impact_adjusted"] = f"proposed {revision.impact}, limited to {impact.value}"
    if revision.urgency is not None:
        urgency = triage.clamp(
            revision.urgency, row.baseline_urgency, settings.incident_urgency_max_deviation
        )
        row.urgency = urgency.value
        reasons.pop("urgency_adjusted", None)
        if urgency.adjusted:
            reasons["urgency_adjusted"] = f"proposed {revision.urgency}, limited to {urgency.value}"
    reasons.update({k: str(v)[:300] for k, v in revision.reasons.items()})
    row.reasons = reasons
    row.version = row.version + 1
    row.updated_at = now
    await session.flush()
    return _view(row, await _group_name(session, row.assignment_group_id))


async def cancel_draft(
    session: AsyncSession, ctx: DraftContext, draft_id: uuid.UUID, *, now: datetime
) -> DraftView:
    row = await _pending(session, ctx, draft_id, now)
    row.status = DraftStatus.CANCELLED
    row.updated_at = now
    await session.flush()
    return _view(row, await _group_name(session, row.assignment_group_id))
