# app/api/routes/integrations.py
"""Endpoints called by other systems, not by the mobile app or the portal.

    POST /api/v1/integrations/glpi/webhooks/{itemtype}/{event}

GLPI webhooks (Phase 17). No X-Site-Id and no X-User: GLPI proves itself with the
HMAC signature (X-GLPI-Signature over body + X-GLPI-Timestamp). The route answers
202 within milliseconds: it only records the ticket in itsm_inbox; the sync runs
later (scripts.itsm_sync now, the worker from Phase 18).
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request, status

from app.api.deps import ClockDep, SessionDep, SettingsDep
from app.api.schemas.integrations import WebhookAck
from app.itsm.webhooks import (
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookTooLarge,
    check_signature,
    check_timestamp,
    record_delivery,
)

router = APIRouter(prefix="/integrations/glpi", tags=["integrations"])

WEBHOOK_ACTOR = "glpi-webhook"


@router.post(
    "/webhooks/{itemtype}/{event}",
    response_model=WebhookAck,
    status_code=status.HTTP_202_ACCEPTED,
)
async def receive_glpi_webhook(
    itemtype: str,
    event: str,
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    clock: ClockDep,
) -> WebhookAck:
    request.state.actor = WEBHOOK_ACTOR
    limit = settings.itsm_webhook_max_body_bytes
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise WebhookTooLarge(f"The body is larger than {limit} bytes.")
    body = await request.body()
    if len(body) > limit:
        raise WebhookTooLarge(f"The body is larger than {limit} bytes.")
    secret = settings.glpi_webhook_secret
    timestamp = request.headers.get(TIMESTAMP_HEADER)
    check_signature(
        secret.get_secret_value() if secret else None,
        body,
        request.headers.get(SIGNATURE_HEADER),
        timestamp,
    )
    now = clock.now()
    glpi_time = check_timestamp(
        timestamp or "",
        now,
        max_age=timedelta(seconds=settings.itsm_webhook_max_age_seconds),
        max_future=timedelta(seconds=settings.itsm_webhook_max_future_seconds),
    )
    result = await record_delivery(
        session, path_itemtype=itemtype, path_event=event, body=body, glpi_time=glpi_time, now=now
    )
    return WebhookAck(
        accepted=result.accepted,
        glpi_ticket_id=result.glpi_ticket_id,
        inbox_id=result.inbox_id,
        merged_count=result.merged_count,
        reason=result.reason,
    )
