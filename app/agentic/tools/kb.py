# app/agentic/tools/kb.py
"""KB tools (KB Guide): find and read articles, and walk one guide step by step
(Agentic Phase 3 retrieval and guide state). Article text is untrusted data."""

from __future__ import annotations

from typing import Literal

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agentic.kb.articles import STAFF, Article
from app.agentic.kb.guide import (
    GuideContext,
    GuideError,
    GuideView,
    end_guide,
    get_guide,
    record_outcome,
    start_guide,
)
from app.agentic.kb.retrieval import find_articles
from app.agentic.store_context.queries import database_now
from app.agentic.tools.base import (
    Args,
    ToolContext,
    ToolFailure,
    ToolOutput,
    parse_uuid,
    require_chat,
)
from app.agentic.values import ErrorCode, KbArticleStatus
from app.models import AgentKbArticle, Device, NetworkElement

GUIDE_UNTRUSTED = ["step.instruction", "step.ask", "steps_tried[].instruction", "title"]


class FindArgs(Args):
    element_type: str | None = Field(default=None, max_length=40)
    fault_code: str | None = Field(default=None, max_length=40)
    kb_tags: list[str] = Field(default_factory=list, max_length=10)
    query: str | None = Field(default=None, max_length=200)


class ArticleArgs(Args):
    kb_ref: str = Field(pattern=r"^KB-[A-Z0-9]+-[0-9]{3}$")


class StartArgs(Args):
    kb_ref: str = Field(pattern=r"^KB-[A-Z0-9]+-[0-9]{3}$")
    subject_node_id: str | None = Field(default=None, max_length=64)
    finding_ids: list[str] = Field(default_factory=list, max_length=10)


class GuideArgs(Args):
    guide_id: str = Field(min_length=1, max_length=64)


class OutcomeArgs(Args):
    guide_id: str = Field(min_length=1, max_length=64)
    step_id: str = Field(pattern=r"^S[0-9]{1,3}$")
    outcome: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=40)
    state_version: int = Field(ge=1)
    reply: str | None = Field(default=None, max_length=500)


class EndArgs(Args):
    guide_id: str = Field(min_length=1, max_length=64)
    reason: Literal["user_cannot_do", "abandoned"]


def _guide_ctx(ctx: ToolContext) -> GuideContext:
    return GuideContext(ctx.store_id, ctx.identity.user_id, ctx.chat_session_id())


def _guide_failure(error: GuideError) -> ToolFailure:
    message = error.message
    current = error.current
    if current is not None:
        step = current.step.step_id if current.step else "none"
        message += (
            f" (now: status {current.status}, step {step}, "
            f"state_version {current.state_version}; read kb_guide_get_state)"
        )
    return ToolFailure(error.code, message)


async def _guide_output(session: AsyncSession, view: GuideView) -> ToolOutput:
    return ToolOutput(view.model_dump(mode="json"), await database_now(session), GUIDE_UNTRUSTED)


async def kb_find_articles(session: AsyncSession, ctx: ToolContext, args: FindArgs) -> ToolOutput:
    if not (args.fault_code or args.kb_tags or (args.query and args.query.strip())):
        raise ToolFailure(ErrorCode.INVALID_INPUT, "give fault_code, kb_tags or query")
    result = await find_articles(
        session,
        element_type=args.element_type,
        fault_code=args.fault_code,
        kb_tags=args.kb_tags,
        query=args.query,
        embedder=ctx.embedder,
        top_k=ctx.settings.kb_search_top_k,
        min_similarity=ctx.settings.kb_search_min_score,
        rrf_k=ctx.settings.kb_search_rrf_k,
    )
    return ToolOutput(
        {
            "candidates": [
                {
                    "kb_ref": c.kb_ref,
                    "version": c.version,
                    "title": c.title,
                    "summary": c.summary,
                    "score": c.score,
                    "matched_by": c.matched_by,
                }
                for c in result.candidates[: ctx.settings.kb_max_candidates_shown]
            ],
            "warnings": result.warnings,
        },
        await database_now(session),
        ["candidates[].title", "candidates[].summary"],
    )


async def kb_get_article(session: AsyncSession, ctx: ToolContext, args: ArticleArgs) -> ToolOutput:
    row = (
        await session.execute(
            select(AgentKbArticle.version, AgentKbArticle.content).where(
                AgentKbArticle.kb_ref == args.kb_ref,
                AgentKbArticle.status == KbArticleStatus.PUBLISHED,
            )
        )
    ).one_or_none()
    if row is None:
        raise ToolFailure(ErrorCode.NOT_FOUND, f"no published article {args.kb_ref}")
    article = Article.model_validate(row.content)
    outline = [
        {"step_id": step_id, "ask": step.ask, "outcomes": list(step.outcomes)}
        for step_id, step in article.steps.items()
        if article.step_audience(step_id) == STAFF
    ]
    return ToolOutput(
        {
            "kb_ref": article.kb_ref,
            "version": row.version,
            "title": article.title,
            "summary": article.summary,
            "applies_to": article.applies_to,
            "fault_codes": article.fault_codes,
            "start": article.start,
            "estimated_minutes": article.estimated_minutes,
            "outline": outline,
        },
        await database_now(session),
        ["title", "summary", "outline[].ask"],
    )


async def kb_guide_start(session: AsyncSession, ctx: ToolContext, args: StartArgs) -> ToolOutput:
    await require_chat(session, ctx)
    device_id = element_id = None
    if args.subject_node_id:
        node_id = parse_uuid(args.subject_node_id, "subject_node_id")
        device_id = await session.scalar(
            select(Device.id).where(Device.id == node_id, Device.store_id == ctx.store_id)
        )
        if device_id is None:
            element_id = await session.scalar(
                select(NetworkElement.id).where(
                    NetworkElement.id == node_id, NetworkElement.store_id == ctx.store_id
                )
            )
            if element_id is None:
                raise ToolFailure(ErrorCode.NOT_FOUND, "subject node is not in this store")
    try:
        view = await start_guide(
            session,
            _guide_ctx(ctx),
            kb_ref=args.kb_ref,
            now=ctx.now,
            subject_device_id=device_id,
            subject_element_id=element_id,
            finding_ids=args.finding_ids,
        )
    except GuideError as exc:
        raise _guide_failure(exc) from exc
    return await _guide_output(session, view)


async def kb_guide_get_state(
    session: AsyncSession, ctx: ToolContext, args: GuideArgs
) -> ToolOutput:
    try:
        view = await get_guide(
            session,
            _guide_ctx(ctx),
            parse_uuid(args.guide_id, "guide_id"),
            now=ctx.now,
            abandon_minutes=ctx.settings.kb_guide_abandon_minutes,
        )
    except GuideError as exc:
        raise _guide_failure(exc) from exc
    return await _guide_output(session, view)


async def kb_guide_record_outcome(
    session: AsyncSession, ctx: ToolContext, args: OutcomeArgs
) -> ToolOutput:
    try:
        view = await record_outcome(
            session,
            _guide_ctx(ctx),
            parse_uuid(args.guide_id, "guide_id"),
            step_id=args.step_id,
            outcome=args.outcome,
            state_version=args.state_version,
            now=ctx.now,
            max_clarifications=ctx.settings.kb_guide_max_clarifications,
            abandon_minutes=ctx.settings.kb_guide_abandon_minutes,
            reply=args.reply,
        )
    except GuideError as exc:
        raise _guide_failure(exc) from exc
    return await _guide_output(session, view)


async def kb_guide_end(session: AsyncSession, ctx: ToolContext, args: EndArgs) -> ToolOutput:
    try:
        view = await end_guide(
            session,
            _guide_ctx(ctx),
            parse_uuid(args.guide_id, "guide_id"),
            now=ctx.now,
            reason=args.reason,
        )
    except GuideError as exc:
        raise _guide_failure(exc) from exc
    return await _guide_output(session, view)
