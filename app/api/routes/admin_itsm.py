# app/api/routes/admin_itsm.py
"""Admin portal: health of the GLPI sync (no X-Site-Id; read-only).

GET /api/v1/admin/itsm/status    the same report as scripts.itsm_status, as JSON

Built from the database only (never calls GLPI), so it answers fast and also while
GLPI is down. Like the other portal routes it is not authenticated yet (Entra ID later).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import ClockDep, SessionDep, SettingsDep
from app.api.schemas.itsm_status import ItsmStatus
from app.itsm.status import collect_status

router = APIRouter(prefix="/admin/itsm", tags=["admin: itsm"])


@router.get("/status", response_model=ItsmStatus)
async def itsm_status(session: SessionDep, settings: SettingsDep, clock: ClockDep) -> ItsmStatus:
    report = await collect_status(session, now=clock.now(), settings=settings)
    return ItsmStatus.model_validate(
        {
            "checked_at": report.checked_at,
            "verdict": report.verdict,
            "checks": report.checks,
            "reconcile": report.reconcile,
            "inbox": report.inbox,
            "problems": report.problems,
            "webhooks": report.webhooks,
            "links": report.links,
            "journal": report.journal,
        }
    )
