# app/api/router.py
"""Collects the business routers under one versioned prefix.

Each domain adds its router here. Health routes are NOT under this prefix;
they stay at the root (/health).
"""

from __future__ import annotations

from fastapi import APIRouter

from app.agentic.gateway.routes import router as chat_router
from app.api.routes import (
    actions,
    admin_itsm,
    alerts,
    appointments,
    devices,
    incidents,
    integrations,
    network,
    notifications,
    portal_notifications,
    store,
)

API_PREFIX = "/api/v1"

api_router = APIRouter()
api_router.include_router(store.router)
api_router.include_router(network.router)
api_router.include_router(devices.router)
api_router.include_router(incidents.router)
api_router.include_router(appointments.router)
api_router.include_router(alerts.router)
api_router.include_router(actions.router)
api_router.include_router(notifications.router)
api_router.include_router(portal_notifications.router)
api_router.include_router(integrations.router)
api_router.include_router(admin_itsm.router)
api_router.include_router(chat_router)
