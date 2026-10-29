# tests/test_config.py
import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_accepts_asyncpg_url() -> None:
    s = Settings(database_url="postgresql+asyncpg://u:p@localhost/db", _env_file=None)
    assert s.database_url.startswith("postgresql+asyncpg://")


def test_rejects_sync_driver_url() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://u:p@localhost/db", _env_file=None)


# --- ITSM / GLPI (Phase 14) ---------------------------------------------------------

GLPI_VALUES = {
    "glpi_v2_url": "http://localhost:8080/api.php/v2",
    "glpi_oauth_token_url": "http://localhost:8080/api.php/token",
    "glpi_oauth_client_id": "client-id",
    "glpi_oauth_client_secret": "client-secret",
    "glpi_api_password": "api-password",
    "glpi_v1_url": "http://localhost:8080/apirest.php",
    "glpi_user_token": "user-token",
    "glpi_app_token": "app-token",
}


def _settings(**values: object) -> Settings:
    return Settings(database_url="postgresql+asyncpg://u:p@localhost/db", _env_file=None, **values)


def test_itsm_defaults() -> None:
    s = _settings()
    assert s.itsm_provider == "glpi"
    assert s.glpi_api_username == "ric.api"
    assert s.glpi_timezone == "UTC"
    assert s.itsm_reconcile_interval_seconds == 900
    assert s.itsm_reconcile_overlap_seconds == 300


def test_itsm_blank_values_are_none() -> None:
    s = _settings(glpi_v2_url="  ", glpi_app_token="", glpi_webhook_secret=" ")
    assert s.glpi_v2_url is None
    assert s.glpi_app_token is None
    assert s.glpi_webhook_secret is None


def test_itsm_rejects_unknown_timezone_and_provider() -> None:
    with pytest.raises(ValidationError):
        _settings(glpi_timezone="Mars/Olympus")
    with pytest.raises(ValidationError):
        _settings(itsm_provider="servicenow")


def test_glpi_config_lists_missing_values() -> None:
    from app.itsm.errors import ItsmConfigError
    from app.itsm.factory import glpi_config

    with pytest.raises(ItsmConfigError) as missing:
        glpi_config(_settings(glpi_v2_url="http://localhost:8080/api.php/v2"))
    message = str(missing.value)
    assert "GLPI_OAUTH_TOKEN_URL" in message and "GLPI_USER_TOKEN" in message
    assert "GLPI_V2_URL" not in message


def test_glpi_config_complete() -> None:
    from app.itsm.factory import glpi_config

    config = glpi_config(_settings(**GLPI_VALUES))
    assert config.username == "ric.api"
    assert config.client_secret == "client-secret"
    assert config.app_token == "app-token"
    assert config.timezone == "UTC"
