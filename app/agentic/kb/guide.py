# app/agentic/kb/guide.py
"""Guided troubleshooting state: one approved article, one step at a time.

The KB Guide agent (Agentic Phase 8) only picks an outcome for the current step;
this service decides where that leads and refuses anything else. Every move is
checked, in order:
    1. the guide belongs to this store, user and chat session (trusted context)
    2. the guide is in progress
    3. the step named is the current step
    4. state_version matches (a retried / duplicate call cannot move twice)
    5. the outcome is one of the step's outcomes, or unclear / user_cannot_do
    6. the next step is for store staff; a technician step ends needs_technician
The article version is pinned when the guide starts. One guide in progress per chat
session (starting another abandons the first). A guide idle for
KB_GUIDE_ABANDON_MINUTES is abandoned. An ended guide row never changes: a resolved
guide is the trace of a self-fix (no incident).

Runs inside the caller's transaction; never commits. Works with the app login and,
later, with the MCP login ric_mcp (insert/update on agent_kb_guide_sessions only).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.kb.articles import (
    STAFF,
    UNCLEAR,
    UNIVERSAL_OUTCOMES,
    USER_CANNOT_DO,
    Article,
)
from app.agentic.values import ErrorCode, GuideStatus, KbArticleStatus
from app.models import AgentKbArticle, AgentKbGuideSession

NEEDS_TECHNICIAN = GuideStatus.NEEDS_TECHNICIAN.value
MAX_REPLY_CHARS = 500
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_LONG_NUMBER = re.compile(r"\+?\d[\d\s-]{6,}\d")


def mask_personal(reply: str | None) -> str | None:
    """Mask e-mail addresses and long numbers (phones, cards) in a user's reply."""
    if reply is None:
        return None
    masked = _LONG_NUMBER.sub("[number]", _EMAIL.sub("[email]", reply))
    return masked[:MAX_REPLY_CHARS]


# --- pure transition ---------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    outcome: str  # the outcome recorded (unclear may become user_cannot_do)
    next_step: str | None = None
    end: str | None = None
    clarify: bool = False


def decide(
    article: Article, step_id: str, outcome: str, clarifications: int, max_clarifications: int
) -> Transition:
    if outcome == UNCLEAR:
        if clarifications >= max_clarifications:
            return Transition(USER_CANNOT_DO, end=NEEDS_TECHNICIAN)
        return Transition(UNCLEAR, clarify=True)
    if outcome == USER_CANNOT_DO:
        return Transition(USER_CANNOT_DO, end=NEEDS_TECHNICIAN)
    target = article.steps[step_id].outcomes[outcome]
    if target.end is not None:
        return Transition(outcome, end=target.end)
    assert target.next is not None
    if article.step_audience(target.next) != STAFF:
        return Transition(outcome, end=NEEDS_TECHNICIAN)
    return Transition(outcome, next_step=target.next)


# --- views ----------------------------------------------------------------------------------


class _View(BaseModel):
    model_config = ConfigDict(frozen=True)


class OutcomeOption(_View):
    key: str
    label: str


class StepView(_View):
    step_id: str
    step_no: int
    instruction: str  # verbatim from the article
    ask: str  # verbatim from the article
    options: list[OutcomeOption]


class StepTried(_View):
    step_id: str
    instruction: str
    outcome: str
    outcome_label: str


class GuideView(_View):
    guide_id: str
    status: GuideStatus
    kb_ref: str
    kb_version: int
    title: str
    state_version: int
    clarifications: int
    step: StepView | None = None  # only while in progress
    steps_tried: list[StepTried] = Field(default_factory=list)
    finding_ids: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class GuideContext:
    """Who is asking. From the gateway / the signed token, never from a model."""

    store_id: uuid.UUID
    user_id: str
    chat_session_id: uuid.UUID


class GuideError(Exception):
    def __init__(self, code: ErrorCode, message: str, current: GuideView | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.current = current


def _label(article: Article, step_id: str, outcome: str) -> str:
    if outcome in UNIVERSAL_OUTCOMES:
        return UNIVERSAL_OUTCOMES[outcome]
    return article.steps[step_id].outcomes[outcome].label


def _steps_tried(article: Article, history: Sequence[dict[str, Any]]) -> list[StepTried]:
    return [
        StepTried(
            step_id=entry["step_id"],
            instruction=article.steps[entry["step_id"]].instruction,
            outcome=entry["outcome"],
            outcome_label=_label(article, entry["step_id"], entry["outcome"]),
        )
        for entry in history
        if entry["outcome"] != UNCLEAR
    ]


def build_view(row: AgentKbGuideSession, article: Article) -> GuideView:
    tried = _steps_tried(article, row.history or [])
    step = None
    if row.status == GuideStatus.IN_PROGRESS:
        current = article.steps[row.current_step]
        options = [OutcomeOption(key=k, label=o.label) for k, o in current.outcomes.items()]
        options += [OutcomeOption(key=k, label=v) for k, v in UNIVERSAL_OUTCOMES.items()]
        step = StepView(
            step_id=row.current_step,
            step_no=len({t.step_id for t in tried} - {row.current_step}) + 1,
            instruction=current.instruction,
            ask=current.ask,
            options=options,
        )
    return GuideView(
        guide_id=str(row.id),
        status=GuideStatus(row.status),
        kb_ref=row.kb_ref,
        kb_version=row.kb_version,
        title=article.title,
        state_version=row.state_version,
        clarifications=row.clarifications,
        step=step,
        steps_tried=tried,
        finding_ids=list(row.finding_ids or []),
    )


# --- database --------------------------------------------------------------------------------


async def _article(session: AsyncSession, article_id: uuid.UUID) -> Article:
    content = await session.scalar(
        select(AgentKbArticle.content).where(AgentKbArticle.id == article_id)
    )
    if content is None:
        raise GuideError(ErrorCode.NOT_FOUND, "the article of this guide no longer exists")
    return Article.model_validate(content)


async def _load(
    session: AsyncSession, ctx: GuideContext, guide_id: uuid.UUID, *, lock: bool
) -> AgentKbGuideSession:
    query = select(AgentKbGuideSession).where(
        AgentKbGuideSession.id == guide_id,
        AgentKbGuideSession.store_id == ctx.store_id,
        AgentKbGuideSession.user_id == ctx.user_id,
        AgentKbGuideSession.chat_session_id == ctx.chat_session_id,
    )
    if lock:
        query = query.with_for_update()
    # populate_existing: a row already in this session (e.g. abandoned by start_guide's
    # bulk update) is refreshed from the database instead of returned stale.
    row = await session.scalar(query.execution_options(populate_existing=True))
    if row is None:
        raise GuideError(ErrorCode.NOT_FOUND, f"guide {guide_id} not found")
    return row


def _end(row: AgentKbGuideSession, status: str, now: datetime) -> None:
    row.status = status
    row.ended_at = now
    row.updated_at = now
    row.state_version = row.state_version + 1


def _abandon_if_idle(row: AgentKbGuideSession, now: datetime, abandon_minutes: int) -> bool:
    if row.status != GuideStatus.IN_PROGRESS:
        return False
    if now - row.updated_at <= timedelta(minutes=abandon_minutes):
        return False
    _end(row, GuideStatus.ABANDONED, now)
    return True


async def start_guide(
    session: AsyncSession,
    ctx: GuideContext,
    *,
    kb_ref: str,
    now: datetime,
    subject_device_id: uuid.UUID | None = None,
    subject_element_id: uuid.UUID | None = None,
    finding_ids: Sequence[str] = (),
) -> GuideView:
    """Start the published version of kb_ref; ends any other guide of this chat."""
    if subject_device_id is not None and subject_element_id is not None:
        raise GuideError(ErrorCode.INVALID_INPUT, "a guide has at most one subject")
    published = (
        await session.execute(
            select(AgentKbArticle.id, AgentKbArticle.version, AgentKbArticle.content).where(
                AgentKbArticle.kb_ref == kb_ref,
                AgentKbArticle.status == KbArticleStatus.PUBLISHED,
            )
        )
    ).one_or_none()
    if published is None:
        raise GuideError(ErrorCode.NOT_FOUND, f"no published article {kb_ref}")
    article = Article.model_validate(published.content)
    await session.execute(
        update(AgentKbGuideSession)
        .where(
            AgentKbGuideSession.chat_session_id == ctx.chat_session_id,
            AgentKbGuideSession.store_id == ctx.store_id,
            AgentKbGuideSession.status == GuideStatus.IN_PROGRESS,
        )
        .values(
            status=GuideStatus.ABANDONED,
            ended_at=now,
            state_version=AgentKbGuideSession.state_version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    row = AgentKbGuideSession(
        id=uuid.uuid4(),
        chat_session_id=ctx.chat_session_id,
        store_id=ctx.store_id,
        user_id=ctx.user_id,
        article_id=published.id,
        kb_ref=kb_ref,
        kb_version=published.version,
        subject_device_id=subject_device_id,
        subject_element_id=subject_element_id,
        finding_ids=list(finding_ids),
        current_step=article.start,
        status=GuideStatus.IN_PROGRESS,
        clarifications=0,
        history=[],
        state_version=1,
        ended_at=None,
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    await session.flush()
    return build_view(row, article)


async def get_guide(
    session: AsyncSession,
    ctx: GuideContext,
    guide_id: uuid.UUID,
    *,
    now: datetime,
    abandon_minutes: int,
) -> GuideView:
    row = await _load(session, ctx, guide_id, lock=True)
    article = await _article(session, row.article_id)
    if _abandon_if_idle(row, now, abandon_minutes):
        await session.flush()
    return build_view(row, article)


async def record_outcome(
    session: AsyncSession,
    ctx: GuideContext,
    guide_id: uuid.UUID,
    *,
    step_id: str,
    outcome: str,
    state_version: int,
    now: datetime,
    max_clarifications: int,
    abandon_minutes: int,
    reply: str | None = None,
) -> GuideView:
    row = await _load(session, ctx, guide_id, lock=True)
    article = await _article(session, row.article_id)
    if _abandon_if_idle(row, now, abandon_minutes):
        await session.flush()
        problem = "the guide was idle too long and has been abandoned"
    elif row.status != GuideStatus.IN_PROGRESS:
        problem = f"the guide has ended ({row.status})"
    elif step_id != row.current_step:
        problem = f"the current step is {row.current_step}, not {step_id}"
    elif state_version != row.state_version:
        problem = f"state_version is {row.state_version}, not {state_version}"
    else:
        problem = None
    if problem is not None:
        raise GuideError(ErrorCode.CONFLICT, problem, build_view(row, article))
    allowed = set(article.steps[step_id].outcomes) | set(UNIVERSAL_OUTCOMES)
    if outcome not in allowed:
        raise GuideError(
            ErrorCode.INVALID_INPUT,
            f"outcome {outcome!r} is not one of {sorted(allowed)}",
            build_view(row, article),
        )

    move = decide(article, step_id, outcome, row.clarifications, max_clarifications)
    entry = {
        "step_id": step_id,
        "outcome": move.outcome,
        "reply": mask_personal(reply),
        "at": now.isoformat(),
    }
    row.history = [*(row.history or []), entry]
    if move.end is not None:
        _end(row, move.end, now)
    else:
        if move.clarify:
            row.clarifications = row.clarifications + 1
        else:
            row.current_step = move.next_step
            row.clarifications = 0
        row.updated_at = now
        row.state_version = row.state_version + 1
    await session.flush()
    return build_view(row, article)


async def end_guide(
    session: AsyncSession,
    ctx: GuideContext,
    guide_id: uuid.UUID,
    *,
    now: datetime,
    reason: str,
) -> GuideView:
    """reason: user_cannot_do (-> needs_technician) or abandoned."""
    endings = {USER_CANNOT_DO: NEEDS_TECHNICIAN, "abandoned": GuideStatus.ABANDONED.value}
    status = endings.get(reason)
    if status is None:
        raise GuideError(ErrorCode.INVALID_INPUT, "reason must be user_cannot_do or abandoned")
    row = await _load(session, ctx, guide_id, lock=True)
    article = await _article(session, row.article_id)
    if row.status != GuideStatus.IN_PROGRESS:
        problem = f"the guide has ended ({row.status})"
        raise GuideError(ErrorCode.CONFLICT, problem, build_view(row, article))
    if reason == USER_CANNOT_DO:
        entry = {
            "step_id": row.current_step,
            "outcome": USER_CANNOT_DO,
            "reply": None,
            "at": now.isoformat(),
        }
        row.history = [*(row.history or []), entry]
    _end(row, status, now)
    await session.flush()
    return build_view(row, article)
