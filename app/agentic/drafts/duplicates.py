# app/agentic/drafts/duplicates.py
"""Is someone already working on this? Checked before every draft (and again at
Confirm, Agentic Phase 10). Compares with the store's ACTIVE incidents and with
other chats' drafts.

strong    an active incident's main CI is the finding's root, an impacted node or
          a node on its evidence path, or the guide's subject (the same match as
          "covered by INC..." in the snapshot); or an active incident was raised
          from an earlier draft with the same finding id
possible  an active incident of the same element type (or either side without a
          CI) opened in the last INCIDENT_DUP_LOOKBACK_HOURS whose title shares
          >= INCIDENT_DUP_TEXT_MIN_SCORE of its words; or another chat has a
          pending draft for the same finding or CI
none      nothing matched
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from sqlalchemy import any_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.common.types import Finding, NodeRef
from app.agentic.drafts.triage import overlap
from app.agentic.store_context.assemble import StoreSnapshot
from app.agentic.values import DraftStatus
from app.models import AgentIncidentDraft, Incident
from app.models.values import ACTIVE_INCIDENT_STATES

DuplicateLevel = Literal["strong", "possible", "none"]


@dataclass(frozen=True)
class DuplicateMatch:
    level: Literal["strong", "possible"]
    reason: str
    incident_number: str | None = None
    short_description: str | None = None
    draft_id: str | None = None


@dataclass
class DuplicateCheck:
    result: DuplicateLevel = "none"
    matches: list[DuplicateMatch] = field(default_factory=list)

    def as_json(self) -> dict[str, object]:
        return {
            "result": self.result,
            "matches": [
                {
                    "level": m.level,
                    "reason": m.reason,
                    "incident_number": m.incident_number,
                    "short_description": m.short_description,
                    "draft_id": m.draft_id,
                }
                for m in self.matches
            ],
        }


def related_node_ids(finding: Finding | None, subject: NodeRef | None) -> set[str]:
    ids: set[str] = set()
    if finding is not None:
        ids.add(finding.root_node.id)
        ids |= {n.id for n in finding.impacted_nodes}
        ids |= {step.node.id for step in finding.evidence_path}
    if subject is not None:
        ids.add(subject.id)
    return ids


async def check_duplicates(
    session: AsyncSession,
    snapshot: StoreSnapshot,
    *,
    store_id: uuid.UUID,
    chat_session_id: uuid.UUID,
    finding: Finding | None,
    subject: NodeRef | None,
    text: str,
    now: datetime,
    lookback_hours: int,
    min_score: float,
) -> DuplicateCheck:
    check = DuplicateCheck()
    related = related_node_ids(finding, subject)
    affected = finding.root_node if finding is not None else subject
    seen: set[str] = set()

    for incident in snapshot.incidents:
        if incident.affected is not None and incident.affected.id in related:
            check.matches.append(
                DuplicateMatch(
                    "strong",
                    f"already raised for {incident.affected.name}",
                    incident.number,
                    incident.short_description,
                )
            )
            seen.add(incident.number)

    if finding is not None:
        rows = (
            await session.execute(
                select(Incident.number, Incident.short_description)
                .join(AgentIncidentDraft, AgentIncidentDraft.incident_id == Incident.id)
                .where(
                    AgentIncidentDraft.store_id == store_id,
                    AgentIncidentDraft.status == DraftStatus.CONFIRMED,
                    finding.finding_id == any_(AgentIncidentDraft.finding_ids),
                    Incident.state.in_(ACTIVE_INCIDENT_STATES),
                )
            )
        ).all()
        for row in rows:
            if row.number not in seen:
                check.matches.append(
                    DuplicateMatch(
                        "strong",
                        "raised from the assistant for the same finding",
                        row.number,
                        row.short_description,
                    )
                )
                seen.add(row.number)

    since = now - timedelta(hours=lookback_hours)
    for incident in snapshot.incidents:
        if incident.number in seen or incident.opened_at < since:
            continue
        same_type = (
            affected is None
            or incident.affected is None
            or incident.affected.element_type == affected.element_type
        )
        score = overlap(text, incident.short_description)
        if same_type and score >= min_score:
            check.matches.append(
                DuplicateMatch(
                    "possible",
                    f"similar title ({score:.2f}) opened recently",
                    incident.number,
                    incident.short_description,
                )
            )

    pending = (
        await session.execute(
            select(
                AgentIncidentDraft.id,
                AgentIncidentDraft.short_description,
                AgentIncidentDraft.finding_ids,
                AgentIncidentDraft.cmdb_ci_device_id,
                AgentIncidentDraft.cmdb_ci_network_element_id,
            ).where(
                AgentIncidentDraft.store_id == store_id,
                AgentIncidentDraft.status == DraftStatus.PENDING,
                AgentIncidentDraft.expires_at > now,
                AgentIncidentDraft.chat_session_id != chat_session_id,
            )
        )
    ).all()
    for row in pending:
        ci = row.cmdb_ci_device_id or row.cmdb_ci_network_element_id
        same_finding = finding is not None and finding.finding_id in (row.finding_ids or [])
        same_ci = ci is not None and affected is not None and str(ci) == affected.id
        if same_finding or same_ci:
            check.matches.append(
                DuplicateMatch(
                    "possible",
                    "another chat has a pending draft for this",
                    short_description=row.short_description,
                    draft_id=str(row.id),
                )
            )

    levels = {m.level for m in check.matches}
    check.result = "strong" if "strong" in levels else "possible" if levels else "none"
    return check


def numbers(check: DuplicateCheck) -> Sequence[str]:
    return [m.incident_number for m in check.matches if m.incident_number]
