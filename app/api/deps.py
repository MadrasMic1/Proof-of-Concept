# app/api/deps.py
"""Shared FastAPI dependencies.

The engine, session factory and clock are created once per process and stored
on app.state; these dependencies hand them to each request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock
from app.core.config import Settings
from app.itsm.glpi_client import GlpiClient


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_clock(request: Request) -> Clock:
    return request.app.state.clock


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, always closed afterwards.

    This dependency never commits. A service that writes calls commit() itself,
    so the success response is only sent after the data is saved. On error the
    transaction is rolled back; read-only requests simply close the session.
    """
    factory = request.app.state.session_factory
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
ClockDep = Annotated[Clock, Depends(get_clock)]


def get_itsm_client(request: Request) -> GlpiClient | None:
    """The GLPI client created at startup; None when the GLPI settings are incomplete."""
    return getattr(request.app.state, "itsm_client", None)


ItsmClientDep = Annotated[GlpiClient | None, Depends(get_itsm_client)]
