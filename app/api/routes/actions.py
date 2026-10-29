# app/api/routes/actions.py
"""Store actions (all need X-Site-Id).

GET  /api/v1/actions                          open actions + answered in the last 24 h
POST /api/v1/actions/{action_id}/responses    the store's reply:
     201 a new reply; 200 the same clientRequestId again (the earlier reply)

Stage 2 (Phase 19): the reply to an action of an incident linked to GLPI is posted to
the GLPI ticket FIRST (public follow-up) and recorded in RIC only after GLPI accepted
it: 503 itsm_unavailable / 502 itsm_rejected when it could not be posted (nothing is
recorded; the app may retry with the same clientRequestId). Replies to incidents that
are not linked to GLPI are recorded in RIC only, as before.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Response, status

from app.api.context import MobileActorDep, StoreDep
from app.api.deps import ClockDep, ItsmClientDep, SessionDep
from app.api.schemas.actions import (
    ActionItem,
    ActionReplyInfo,
    ActionReplyRequest,
    ActionReplyResult,
)
from app.core.timefmt import format_when
from app.itsm.responses import post_store_reply
from app.queries.actions import visible_actions
from app.services.action_responses import NewReply, prepare_reply, respond_to_action

router = APIRouter(prefix="/actions", tags=["actions"])


@router.get("", response_model=list[ActionItem])
async def list_actions(store: StoreDep, session: SessionDep, clock: ClockDep) -> list[ActionItem]:
    now = clock.now()
    return [
        ActionItem(
            id=row.id,
            title=row.title,
            detail=row.detail,
            severity=row.severity,
            expected_response=row.expected_response,
            due_by=row.due_by,
            due_by_text=format_when(row.due_by, store.timezone, now) if row.due_by else None,
            status=row.status,
            response=(
                ActionReplyInfo(
                    answer_yes_no=row.reply.answer_yes_no,
                    response_text=row.reply.response_text,
                    responded_by=row.reply.responded_by,
                    responded_at=row.reply.created_at,
                )
                if row.reply
                else None
            ),
            incident_id=row.incident_number,
            created_at=row.created_at,
        )
        for row in await visible_actions(session, store.id, now)
    ]


@router.post(
    "/{action_id}/responses",
    response_model=ActionReplyResult,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Repeated clientRequestId: the earlier reply"},
        502: {"description": "GLPI refused the reply (nothing recorded)"},
        503: {"description": "GLPI did not answer (nothing recorded; retry)"},
    },
)
async def reply_to_action(
    action_id: uuid.UUID,
    body: ActionReplyRequest,
    store: StoreDep,
    actor: MobileActorDep,
    session: SessionDep,
    clock: ClockDep,
    itsm: ItsmClientDep,
    http_response: Response,
) -> ActionReplyResult:
    reply = NewReply(
        client_request_id=body.client_request_id,
        answer_yes_no=body.answer_yes_no,
        response_text=body.response_text,
    )
    plan = await prepare_reply(session, actor, store.id, action_id, reply, clock.now())
    await session.rollback()  # end the read-only transaction before calling GLPI
    followup_id = None
    if not plan.repeated and plan.glpi_ticket_id is not None and plan.text:
        followup_id = await post_store_reply(
            itsm,
            plan.glpi_ticket_id,
            site_code=store.site_code,
            actor_name=actor.actor,
            text=plan.text,
        )
    result = await respond_to_action(
        session,
        actor,
        store.id,
        action_id,
        reply,
        clock.now(),
        glpi_followup_id=followup_id,
    )
    if not result.created:
        http_response.status_code = status.HTTP_200_OK
    saved = result.response
    return ActionReplyResult(
        id=saved.id,
        action_id=saved.action_id,
        response_type=saved.response_type,
        answer_yes_no=saved.answer_yes_no,
        response_text=saved.response_text,
        responded_by=saved.responded_by,
        responded_at=saved.created_at,
        action_status=result.action.status,
    )
