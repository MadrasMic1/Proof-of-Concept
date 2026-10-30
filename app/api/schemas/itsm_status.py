# app/api/schemas/itsm_status.py
"""Response model of GET /api/v1/admin/itsm/status (app/itsm/status.py)."""

from __future__ import annotations

from typing import Any

from app.api.schemas.base import ApiModel, UtcDateTime


class StatusCheck(ApiModel):
    area: str
    level: str  # OK, INFO, WARN, FAIL
    message: str


class ReconcileStatus(ApiModel):
    watermark: UtcDateTime | None
    next_run_at: UtcDateTime
    last_started_at: UtcDateTime | None
    last_success_at: UtcDateTime | None
    consecutive_failures: int
    last_error: str | None
    details: dict[str, Any]


class InboxStatusInfo(ApiModel):
    by_status: dict[str, int]
    by_source: dict[str, int]  # pending rows by source
    due: int
    waiting_retry: int
    oldest_due_received_at: UtcDateTime | None
    stuck_processing: int


class InboxProblem(ApiModel):
    inbox_id: int
    status: str  # failed or conflict
    glpi_ticket_id: int
    incident_number: str | None
    source: str
    attempts: int
    last_error: str | None
    processed_at: UtcDateTime | None


class WebhookStatus(ApiModel):
    last_received_at: UtcDateTime | None
    deliveries_24h: int
    delay_median_seconds: float | None
    delay_max_seconds: float | None


class LinkStatus(ApiModel):
    stores: int
    stores_linked: int
    active_incidents_not_linked: int
    examples: list[str]


class JournalStatus(ApiModel):
    glpi_pending: int
    glpi_failed: int


class ItsmStatus(ApiModel):
    checked_at: UtcDateTime
    verdict: str  # OK, WARN, FAIL
    checks: list[StatusCheck]
    reconcile: ReconcileStatus | None
    inbox: InboxStatusInfo | None
    problems: list[InboxProblem]
    webhooks: WebhookStatus | None
    links: LinkStatus | None
    journal: JournalStatus | None
