# tests/test_itsm_webhooks.py
"""Phase 17: the GLPI webhook endpoint and its checks (test database, no GLPI)."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.api.deps import get_clock
from app.core.clock import FixedClock
from app.core.config import Settings
from app.itsm.glpi_client import GlpiClient, GlpiConfig
from app.itsm.webhooks import (
    WEBHOOK_PATH,
    WebhookNotConfigured,
    WebhookUnauthorized,
    check_signature,
    check_timestamp,
    sign,
    ticket_id_of,
    webhook_url,
)
from app.main import create_app
from app.models import ItsmInbox
from scripts import itsm_webhooks as webhook_script

SECRET = "test-webhook-secret"
NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)
TICKETS = range(970001, 970100)  # test-only GLPI ticket ids, deleted before and after


def payload(itemtype: str, item_id: int, ticket_id: int, parent_type: str = "Ticket") -> dict:
    if itemtype == "ticket":
        return {"event": "new", "item": {"id": ticket_id, "name": "ISP - Primary Link"}}
    return {
        "event": "new",
        "item": {"id": item_id, "itemtype": parent_type, "items_id": ticket_id, "content": "x"},
        "parent_item": {"id": ticket_id, "name": "ISP - Primary Link"},
    }


def signed(body: bytes, at: datetime = NOW, secret: str = SECRET) -> dict[str, str]:
    stamp = str(int(at.timestamp()))
    return {
        "Content-Type": "application/json",
        "X-GLPI-Timestamp": stamp,
        "X-GLPI-Signature": sign(secret, body, stamp),
    }


@pytest.fixture
def app(test_settings: Settings) -> FastAPI:
    settings = test_settings.model_copy(
        update={"glpi_webhook_secret": SecretStr(SECRET), "itsm_webhook_max_body_bytes": 4096}
    )
    app = create_app(settings)
    app.dependency_overrides[get_clock] = lambda: FixedClock(NOW)
    return app


@pytest.fixture(autouse=True)
async def clean_inbox(migrated_db: str) -> AsyncIterator[None]:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    query = delete(ItsmInbox).where(ItsmInbox.glpi_ticket_id.between(TICKETS[0], TICKETS[-1]))
    async with engine.begin() as connection:
        await connection.execute(query)
    yield
    async with engine.begin() as connection:
        await connection.execute(query)
    await engine.dispose()


async def inbox_rows(migrated_db: str, ticket_id: int) -> list[ItsmInbox]:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = (
            await connection.execute(
                select(ItsmInbox.__table__).where(ItsmInbox.glpi_ticket_id == ticket_id)
            )
        ).all()
    await engine.dispose()
    return rows


async def post(client: AsyncClient, path: str, body: bytes, headers: dict[str, str]):
    return await client.post(f"{WEBHOOK_PATH}/{path}", content=body, headers=headers)


# --- route ---------------------------------------------------------------------------------


async def test_burst_is_merged_into_one_pending_row(client: AsyncClient, migrated_db: str) -> None:
    created = json.dumps(payload("ticket", 970001, 970001)).encode()
    first = await post(client, "ticket/new", created, signed(created))
    assert first.status_code == 202, first.text
    ack = first.json()
    assert ack["accepted"] is True and ack["glpiTicketId"] == 970001 and ack["mergedCount"] == 1

    note = json.dumps(payload("itilfollowup", 55, 970001)).encode()
    second = await post(client, "itilfollowup/new", note, signed(note))
    assert second.status_code == 202
    assert second.json()["inboxId"] == ack["inboxId"] and second.json()["mergedCount"] == 2

    rows = await inbox_rows(migrated_db, 970001)
    assert len(rows) == 1
    row = rows[0]
    assert (row.source, row.status, row.merged_count) == ("webhook", "pending", 2)
    assert [e["event"] for e in row.events] == ["Ticket/new", "ITILFollowup/new"]
    assert row.events[1]["item_id"] == 55
    assert row.events[1]["payload"] == payload("itilfollowup", 55, 970001)  # full payload kept
    assert row.events[0]["glpi_time"] == NOW.isoformat()


async def test_followup_with_items_id_only(client: AsyncClient) -> None:
    body = json.dumps({"item": {"id": 56, "itemtype": "Ticket", "items_id": 970002}}).encode()
    response = await post(client, "itilfollowup/new", body, signed(body))
    assert response.json()["glpiTicketId"] == 970002


@pytest.mark.parametrize(
    ("path", "body", "reason"),
    [
        ("computer/new", json.dumps(payload("ticket", 1, 970003)).encode(), "does not handle"),
        ("ticket/delete", json.dumps(payload("ticket", 1, 970003)).encode(), "does not handle"),
        ("itilfollowup/new", json.dumps(payload("f", 9, 970003, "Change")).encode(), "Change"),
        ("ticket/new", b'{"item": {"name": "no id"}}', "no id"),
        ("ticket/new", b"not json", "not JSON"),
    ],
    ids=["unknown-itemtype", "unknown-event", "change-followup", "no-ticket-id", "not-json"],
)
async def test_unusable_deliveries_are_accepted_but_not_queued(
    client: AsyncClient, migrated_db: str, path: str, body: bytes, reason: str
) -> None:
    response = await post(client, path, body, signed(body))
    assert response.status_code == 202
    ack = response.json()
    assert ack["accepted"] is False and reason in ack["reason"]
    assert await inbox_rows(migrated_db, 970003) == []


async def test_bad_signature_and_timestamps_are_rejected(
    client: AsyncClient, migrated_db: str
) -> None:
    body = json.dumps(payload("ticket", 970004, 970004)).encode()
    cases = [
        signed(body, secret="wrong-secret"),
        {"Content-Type": "application/json"},
        signed(body, NOW - timedelta(hours=25)),
        signed(body, NOW + timedelta(minutes=10)),
        {**signed(body), "X-GLPI-Timestamp": "yesterday"},
    ]
    for headers in cases:
        response = await post(client, "ticket/new", body, headers)
        assert response.status_code == 401, response.text
        assert response.json()["error"]["code"] == "unauthorized"
    late_retry = await post(client, "ticket/new", body, signed(body, NOW - timedelta(hours=23)))
    assert late_retry.status_code == 202
    assert len(await inbox_rows(migrated_db, 970004)) == 1


async def test_body_too_large(client: AsyncClient) -> None:
    body = json.dumps({"item": {"id": 970005}, "pad": "x" * 5000}).encode()
    response = await post(client, "ticket/new", body, signed(body))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


async def test_not_configured(test_settings: Settings) -> None:
    app = create_app(test_settings.model_copy(update={"glpi_webhook_secret": None}))
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            body = b"{}"
            response = await post(client, "ticket/new", body, signed(body))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_configured"


# --- checks (pure) ------------------------------------------------------------------------


def test_signature_matches_glpi_layout() -> None:
    body, stamp = b'{"id": 1}', "1761728400"
    check_signature(SECRET, body, sign(SECRET, body, stamp).upper(), stamp)
    with pytest.raises(WebhookUnauthorized):
        check_signature(SECRET, body + b" ", sign(SECRET, body, stamp), stamp)
    with pytest.raises(WebhookNotConfigured):
        check_signature(None, body, "x", stamp)


def test_timestamp_window() -> None:
    window = {"max_age": timedelta(hours=24), "max_future": timedelta(minutes=5)}
    assert check_timestamp(str(int(NOW.timestamp())), NOW, **window) == NOW
    with pytest.raises(WebhookUnauthorized):
        check_timestamp(str(int((NOW - timedelta(days=2)).timestamp())), NOW, **window)


def test_ticket_id_of() -> None:
    assert ticket_id_of("Ticket", {"item": {"id": "7"}}) == (7, None)
    assert ticket_id_of("ITILSolution", {"item": {"id": 3}, "parent_item": {"id": 8}}) == (8, None)
    assert ticket_id_of("ITILFollowup", {"item": {"id": 3, "itemtype": "Problem"}})[0] is None
    assert ticket_id_of("Ticket", ["not", "an", "object"])[0] is None


def test_webhook_url() -> None:
    assert webhook_url("http://host.docker.internal:8000/", "ITILFollowup", "new") == (
        "http://host.docker.internal:8000/api/v1/integrations/glpi/webhooks/itilfollowup/new"
    )


# --- GLPI webhook management (client + script, fake GLPI V1) ------------------------------


class FakeGlpiWebhooks:
    def __init__(self, hooks: list[dict]) -> None:
        self.hooks = hooks
        self.writes: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/apirest.php/")
        if path == "initSession":
            return httpx.Response(200, json={"session_token": "s1"})
        if path == "killSession":
            return httpx.Response(200, json=True)
        if request.method == "GET" and path == "Webhook":
            return httpx.Response(200, json=self.hooks)
        body = json.loads(request.content)["input"]
        self.writes.append((request.method, path, body))
        if request.method == "POST" and path == "Webhook":
            new = {"id": 100 + len(self.hooks), **body}
            self.hooks.append(new)
            return httpx.Response(201, json={"id": new["id"], "message": ""})
        if request.method == "PUT" and path.startswith("Webhook/"):
            hook_id = int(path.split("/")[1])
            for hook in self.hooks:
                if hook["id"] == hook_id:
                    hook.update(body)
            return httpx.Response(200, json=[{str(hook_id): True, "message": ""}])
        return httpx.Response(404, json=["ERROR_ITEM_NOT_FOUND", ""])


GLPI_CONFIG = GlpiConfig(
    v2_url="http://glpi.test/api.php/v2",
    token_url="http://glpi.test/api.php/token",
    client_id="c",
    client_secret="s",
    username="ric.api",
    password="p",
    v1_url="http://glpi.test/apirest.php",
    user_token="u",
    app_token=None,
)


async def run_script(monkeypatch, fake: FakeGlpiWebhooks, *argv: str) -> int:
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        glpi_webhook_secret=SecretStr(SECRET),
    )
    monkeypatch.setattr(webhook_script, "get_settings", lambda: settings)
    monkeypatch.setattr(
        webhook_script,
        "build_itsm_client",
        lambda s: GlpiClient(GLPI_CONFIG, transport=httpx.MockTransport(fake)),
    )
    monkeypatch.setattr(sys, "argv", ["itsm_webhooks", *argv])
    return await webhook_script.main()


async def test_webhook_script_creates_updates_and_disables(monkeypatch) -> None:
    test_hook = {
        "id": 1,
        "name": "RIC test - Ticket new",
        "itemtype": "Ticket",
        "event": "new",
        "url": "http://host.docker.internal:8099/glpi/ticket/new",
        "is_active": 1,
    }
    fake = FakeGlpiWebhooks([test_hook])
    assert await run_script(monkeypatch, fake) == 0
    created = [body for method, _, body in fake.writes if method == "POST"]
    assert [body["name"] for body in created] == [name for name, _, _ in webhook_script.WEBHOOKS]
    note = next(body for body in created if body["itemtype"] == "ITILFollowup")
    assert note["url"].endswith("/api/v1/integrations/glpi/webhooks/itilfollowup/new")
    assert (note["secret"], note["use_default_payload"], note["sent_try"]) == (SECRET, 1, 3)
    assert test_hook["is_active"] == 0  # Phase 4 test webhook switched off

    fake.writes.clear()
    assert await run_script(monkeypatch, fake) == 0  # second run: updates, no new webhook
    assert {method for method, _, _ in fake.writes} == {"PUT"}

    assert await run_script(monkeypatch, fake, "--disable") == 0
    own = [h for h in fake.hooks if h["name"] in webhook_script.OWN_NAMES]
    assert {h["is_active"] for h in own} == {0}
