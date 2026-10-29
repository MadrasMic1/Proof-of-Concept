# app/itsm/factory.py
"""Build the ITSM client from settings (one per process: the API and the worker)."""

from __future__ import annotations

from pydantic import SecretStr

from app.core.config import Settings
from app.itsm.errors import ItsmConfigError
from app.itsm.glpi_client import GlpiClient, GlpiConfig


def _secret(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def glpi_config(settings: Settings) -> GlpiConfig:
    """The GLPI connection settings; ItsmConfigError lists every missing value."""
    required = {
        "GLPI_V2_URL": settings.glpi_v2_url,
        "GLPI_OAUTH_TOKEN_URL": settings.glpi_oauth_token_url,
        "GLPI_OAUTH_CLIENT_ID": settings.glpi_oauth_client_id,
        "GLPI_OAUTH_CLIENT_SECRET": _secret(settings.glpi_oauth_client_secret),
        "GLPI_API_USERNAME": settings.glpi_api_username.strip() or None,
        "GLPI_API_PASSWORD": _secret(settings.glpi_api_password),
        "GLPI_V1_URL": settings.glpi_v1_url,
        "GLPI_USER_TOKEN": _secret(settings.glpi_user_token),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ItsmConfigError(
            "ITSM_PROVIDER=glpi needs these settings in .env: " + ", ".join(missing)
        )
    return GlpiConfig(
        v2_url=str(required["GLPI_V2_URL"]),
        token_url=str(required["GLPI_OAUTH_TOKEN_URL"]),
        client_id=str(required["GLPI_OAUTH_CLIENT_ID"]),
        client_secret=str(required["GLPI_OAUTH_CLIENT_SECRET"]),
        username=str(required["GLPI_API_USERNAME"]),
        password=str(required["GLPI_API_PASSWORD"]),
        v1_url=str(required["GLPI_V1_URL"]),
        user_token=str(required["GLPI_USER_TOKEN"]),
        app_token=_secret(settings.glpi_app_token),
        timezone=settings.glpi_timezone,
        timeout_seconds=settings.glpi_timeout_seconds,
    )


def build_itsm_client(settings: Settings) -> GlpiClient:
    """The client for ITSM_PROVIDER (only 'glpi' exists). Call aclose() when done."""
    return GlpiClient(glpi_config(settings))
