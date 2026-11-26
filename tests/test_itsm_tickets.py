# tests/test_itsm_tickets.py
"""Agentic Phase 8: creating GLPI tickets for new RIC incidents (client part, no GLPI:
httpx.MockTransport)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.itsm.errors import ItsmRejected
from app.itsm.glpi_client import GlpiClient, GlpiConfig
from app.itsm.tickets import GLPI_LEVEL, ticket_content_html

CONFIG = GlpiConfig(
    v2_url="http://glpi.test/api.php/v2",
    token_url="http://glpi.test/api.php/token",
    client_id="c",
    client_secret="s",
    username="ric.api",
    password="p",
    v1_url="http://glpi.test/apirest.php",
    user_token="u",
    app_token="a",
)
CATEGORIES = [
    {"id": 10, "name": "Hardware", "completename": "Hardware"},
    {"id": 11, "name": "RF Gun", "completename": "Hardware > RF Gun"},
    {"id": 20, "name": "Network", "completename": "Network"},
]


def _client(posts: list[tuple[str, dict[str, Any]]], ticket_answer: Any) -> GlpiClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/initSession"):
            return httpx.Response(200, json={"session_token": "sess"})
        if path.endswith("/ITILCategory"):
            return httpx.Response(200, json=CATEGORIES)
        if request.method == "POST":
            posts.append((path.rsplit("/", 1)[-1], json.loads(request.content)))
            if path.endswith("/Ticket"):
                return httpx.Response(201, json=ticket_answer)
            return httpx.Response(201, json={"id": 5, "message": ""})
        return httpx.Response(404, json=["ERROR_ITEM_NOT_FOUND", ""])

    return GlpiClient(CONFIG, transport=httpx.MockTransport(handler))


async def test_create_link_and_categories() -> None:
    posts: list[tuple[str, dict[str, Any]]] = []
    client = _client(posts, {"id": 77, "message": ""})
    try:
        assert await client.find_category_id("hardware", "RF Gun") == 11
        assert await client.find_category_id("Hardware", "Printer") == 10
        assert await client.find_category_id("network", None) == 20
        assert await client.find_category_id("inquiry", None) is None
        ticket = await client.create_ticket({"name": "x", "externalid": "INC00009999"})
        await client.link_item(ticket, "Peripheral", 42)
    finally:
        await client.aclose()
    assert ticket == 77
    assert posts[0] == ("Ticket", {"input": {"name": "x", "externalid": "INC00009999"}})
    assert posts[1] == (
        "Item_Ticket",
        {"input": {"tickets_id": 77, "itemtype": "Peripheral", "items_id": 42}},
    )


async def test_create_without_id_is_rejected() -> None:
    client = _client([], {"message": "no rights"})
    try:
        with pytest.raises(ItsmRejected, match="no id"):
            await client.create_ticket({"name": "x"})
    finally:
        await client.aclose()


def test_levels_and_content() -> None:
    assert GLPI_LEVEL == {1: 4, 2: 3, 3: 2}
    body = ticket_content_html(
        "RF Gun 02 <dead>.\n\n--- RIC evidence ---\nLine 1",
        site_code="03340",
        store_name="Manchester Arndale",
        raised_by="priya.store-manager",
        number="INC00001240",
    )
    assert body.startswith(
        "<p>[Raised from RIC by priya.store-manager - store 03340 Manchester Arndale"
        " - RIC INC00001240]</p>"
    )
    assert "<p>RF Gun 02 &lt;dead&gt;.</p>" in body
    assert "<p>--- RIC evidence ---<br>Line 1</p>" in body
