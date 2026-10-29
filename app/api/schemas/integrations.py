# app/api/schemas/integrations.py
"""Response model of the integration endpoints (GLPI webhooks)."""

from __future__ import annotations

from app.api.schemas.base import ApiModel


class WebhookAck(ApiModel):
    """202 answer to GLPI. accepted=false: the delivery was valid but not usable (reason)."""

    accepted: bool
    glpi_ticket_id: int | None = None
    inbox_id: int | None = None
    merged_count: int | None = None
    reason: str | None = None
