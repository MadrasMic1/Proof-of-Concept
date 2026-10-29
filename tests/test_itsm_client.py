# tests/test_itsm_client.py
"""GlpiClient against a fake GLPI (httpx.MockTransport): no network, no GLPI needed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

from app.itsm.errors import ItsmNotFound, ItsmRejected, ItsmUnavailable
from app.itsm.glpi_client import GlpiClient, GlpiConfig
from app.itsm.snapshot import LinkedItem, TeamMember

CONFIG = GlpiConfig(
    v2_url="http://glpi.test/api.php/v2",
    token_url="http://glpi.test/api.php/token",
    client_id="client-id",
    client_secret="client-secret",
    username="ric.api",
    password="api-password",
    v1_url="http://glpi.test/apirest.php",
    user_token="user-token",
    app_token="app-token",
)

V2_TICKET = {
    "id": 3,
    "name": "ISP - Primary Link",
    "content": "<p>Aruba reports WAN health DOWN.</p>",
    "is_deleted": False,
    "urgency": 3,
    "impact": 4,
    "type": 1,
    "external_id": "INC00001234",
    "date": "2026-10-25T18:20:00+00:00",
    "date_mod": "2026-10-25T18:25:38+00:00",
    "date_solve": None,
    "date_close": None,
    "status": {"id": 4, "name": "Pending"},
    "user_recipient": {"id": 7, "name": "ric.api"},
    "category": {"id": 5, "name": "WAN"},
    "location": {"id": 3, "name": "01234 - Chennai Anna Nagar"},
    "request_type": {"id": 9, "name": "Monitoring"},
    "team": [
        {"role": "assigned", "type": "Group", "id": 1, "name": "Network Ops"},
        {"role": "assigned", "type": "User", "id": 8, "name": "r.kumar"},
    ],
}
V2_TIMELINE = [
    {
        "type": "Followup",
        "item": {
            "id": 21,
            "content": "<p>Confirmed WAN down.</p>",
            "is_private": True,
            "date": "2026-10-25T18:30:00+00:00",
            "date_mod": "2026-10-25T18:30:00+00:00",
            "user": {"id": 8, "name": "r.kumar"},
        },
    },
    {
        "type": "Followup",
        "item": {
            "id": 20,
            "content": "<p>Link is down.</p>",
            "is_private": False,
            "date": "2026-10-25T18:28:00+00:00",
            "date_mod": "2026-10-25T18:28:00+00:00",
            "user": {"id": 8, "name": "r.kumar"},
        },
    },
    {"type": "Task", "item": {"id": 99, "content": "ignored"}},
]
V1_TICKET = {"id": 3, "itilcategories_id": 5, "time_to_resolve": "2026-10-26 07:30:00"}


class FakeGlpi:
    """A tiny GLPI: records every request; behaviour switches per test."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.tokens_issued = 0
        self.sessions_issued = 0
        self.reject_token_once = False
        self.reject_session_once = False
        self.status_override: dict[str, int] = {}
        self.changed_pages: list[list[dict[str, Any]]] = []
        self.followup_answer: tuple[int, Any, dict[str, str]] = (201, {"id": 77}, {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        for prefix, status in self.status_override.items():
            if path.startswith(prefix):
                return httpx.Response(status, json=["ERROR", "forced"])
        if path == "/api.php/token":
            self.tokens_issued += 1
            return httpx.Response(
                200, json={"access_token": f"token-{self.tokens_issued}", "expires_in": 3600}
            )
        if path.startswith("/api.php/v2/"):
            expected = f"Bearer token-{self.tokens_issued}"
            if request.headers.get("Authorization") != expected or self.reject_token_once:
                self.reject_token_once = False
                return httpx.Response(401, json={"title": "Unauthorized"})
            return self.v2(request, path.removeprefix("/api.php/v2/"))
        if path == "/apirest.php/initSession":
            self.sessions_issued += 1
            return httpx.Response(200, json={"session_token": f"session-{self.sessions_issued}"})
        if path.startswith("/apirest.php/"):
            if request.headers.get("App-Token") != "app-token":
                return httpx.Response(400, json=["ERROR_WRONG_APP_TOKEN_PARAMETER", ""])
            expected = f"session-{self.sessions_issued}"
            if request.headers.get("Session-Token") != expected or self.reject_session_once:
                self.reject_session_once = False
                return httpx.Response(401, json=["ERROR_SESSION_TOKEN_INVALID", ""])
            return self.v1(request, path.removeprefix("/apirest.php/"))
        return httpx.Response(404)

    def v2(self, request: httpx.Request, path: str) -> httpx.Response:
        if request.method == "GET" and path == "Assistance/Ticket/3":
            return httpx.Response(200, json=V2_TICKET)
        if request.method == "GET" and path == "Assistance/Ticket/3/Timeline":
            return httpx.Response(200, json=V2_TIMELINE)
        if request.method == "GET" and path == "Assistance/Ticket":
            start = int(request.url.params["start"])
            page = start // int(request.url.params["limit"])
            items = self.changed_pages[page] if page < len(self.changed_pages) else []
            return httpx.Response(206 if items else 200, json=items)
        if request.method == "POST" and path == "Assistance/Ticket/3/Timeline/Followup":
            status, body, headers = self.followup_answer
            return httpx.Response(status, json=body, headers=headers)
        if request.method == "PATCH" and path == "Assistance/Ticket/3":
            return httpx.Response(200, json={"id": 3})
        return httpx.Response(404, json={"title": "Not Found"})

    def v1(self, request: httpx.Request, path: str) -> httpx.Response:
        params = request.url.params
        if path == "Ticket/3":
            return httpx.Response(200, json=V1_TICKET)
        if path == "Ticket/3/Item_Ticket":
            return httpx.Response(
                200,
                json=[{"id": 1, "tickets_id": 3, "itemtype": "NetworkEquipment", "items_id": 41}],
            )
        if path == "PendingReason_Item":
            return httpx.Response(
                200,
                json=[
                    {"id": 4, "itemtype": "Ticket", "items_id": 3, "pendingreasons_id": 2},
                    {"id": 5, "itemtype": "Ticket", "items_id": 30, "pendingreasons_id": 1},
                    {"id": 6, "itemtype": "Change", "items_id": 3, "pendingreasons_id": 1},
                ],
            )
        if path == "PendingReason/2":
            return httpx.Response(200, json={"id": 2, "name": "Awaiting site visit"})
        if path == "ITILCategory/5":
            return httpx.Response(200, json={"id": 5, "name": "WAN", "itilcategories_id": 1})
        if path == "ITILCategory/1":
            return httpx.Response(200, json={"id": 1, "name": "Network", "itilcategories_id": 0})
        if path == "Ticket" and "searchText[externalid]" in params:
            return httpx.Response(
                200,
                json=[
                    {"id": 3, "externalid": "INC00001234"},
                    {"id": 30, "externalid": "INC000012345"},
                ],
            )
        if path == "killSession":
            return httpx.Response(200, json=True)
        return httpx.Response(404, json=["ERROR_ITEM_NOT_FOUND", ""])

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.path}" for r in self.requests]


@pytest.fixture
def fake() -> FakeGlpi:
    return FakeGlpi()


def make_client(fake: FakeGlpi, now: list[float] | None = None) -> GlpiClient:
    clock = now if now is not None else [0.0]
    return GlpiClient(CONFIG, transport=httpx.MockTransport(fake), monotonic=lambda: clock[0])


async def test_snapshot_of_a_pending_ticket(fake: FakeGlpi) -> None:
    client = make_client(fake)
    snapshot = await client.get_ticket_snapshot(3)
    assert snapshot.id == 3 and snapshot.status == 4 and snapshot.type == 1
    assert snapshot.external_id == "INC00001234"
    assert (snapshot.location_id, snapshot.location_name) == (3, "01234 - Chennai Anna Nagar")
    assert snapshot.request_type_name == "Monitoring"
    assert (snapshot.category_name, snapshot.category_parent_name) == ("WAN", "Network")
    assert snapshot.pending_reason_name == "Awaiting site visit"
    assert snapshot.date_mod == datetime(2026, 10, 25, 18, 25, 38, tzinfo=UTC)
    assert snapshot.time_to_resolve == datetime(2026, 10, 26, 7, 30, tzinfo=UTC)
    assert snapshot.team == (
        TeamMember(kind="group", role="assigned", id=1, name="Network Ops"),
        TeamMember(kind="user", role="assigned", id=8, name="r.kumar"),
    )
    assert [f.id for f in snapshot.followups] == [20, 21]
    assert snapshot.followups[1].is_private and snapshot.followups[1].author_name == "r.kumar"
    assert snapshot.solutions == ()
    assert snapshot.linked_items == (LinkedItem(itemtype="NetworkEquipment", item_id=41),)
    # token + 2 V2 + initSession + 2 V1 + pending (2) + categories (2)
    assert client.calls == 10
    await client.get_ticket_snapshot(3)
    assert client.calls == 15  # 5 more: no new token or session; names come from the cache
    await client.aclose()
    assert fake.paths()[-1] == "GET /apirest.php/killSession"


async def test_v2_token_is_renewed_once_on_401(fake: FakeGlpi) -> None:
    client = make_client(fake)
    await client.check_connection()
    fake.reject_token_once = True
    await client.get_v2_json("Assistance/Ticket/3")
    assert fake.tokens_issued == 2
    await client.aclose()


async def test_v2_token_is_renewed_when_expired(fake: FakeGlpi) -> None:
    now = [0.0]
    client = make_client(fake, now)
    await client.get_v2_json("Assistance/Ticket/3")
    now[0] = 3600 - 59
    await client.get_v2_json("Assistance/Ticket/3")
    assert fake.tokens_issued == 2
    await client.aclose()


async def test_v1_session_is_renewed_once_on_401(fake: FakeGlpi) -> None:
    client = make_client(fake)
    await client.check_connection()
    fake.reject_session_once = True
    assert await client.find_ticket_id("INC00001234") == 3
    assert fake.sessions_issued == 2
    await client.aclose()


async def test_errors_are_classified(fake: FakeGlpi) -> None:
    client = make_client(fake)
    with pytest.raises(ItsmNotFound):
        await client.get_ticket_snapshot(404)
    fake.status_override = {"/api.php/v2/Assistance/Ticket/3": 503}
    with pytest.raises(ItsmUnavailable) as unavailable:
        await client.get_ticket_snapshot(3)
    assert unavailable.value.status == 503
    fake.status_override = {"/api.php/v2/Assistance/Ticket/3": 429}
    with pytest.raises(ItsmUnavailable):
        await client.get_ticket_snapshot(3)
    fake.status_override = {"/api.php/v2/Assistance/Ticket/3": 403}
    with pytest.raises(ItsmRejected):
        await client.get_ticket_snapshot(3)
    await client.aclose()


async def test_refused_token_does_not_leak_secrets() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "invalid_grant"})

    client = GlpiClient(CONFIG, transport=httpx.MockTransport(refuse))
    with pytest.raises(ItsmRejected) as refused:
        await client.check_connection()
    text = str(refused.value)
    for secret in ("api-password", "client-secret", "user-token", "app-token"):
        assert secret not in text
    await client.aclose()


async def test_connection_problems_are_unavailable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    for handler in (broken, slow):
        client = GlpiClient(CONFIG, transport=httpx.MockTransport(handler))
        with pytest.raises(ItsmUnavailable):
            await client.check_connection()
        await client.aclose()


async def test_changed_since_pages_filters_and_sorts(fake: FakeGlpi) -> None:
    fake.changed_pages = [
        [
            {"id": n, "type": 1, "date_mod": f"2026-10-28T10:{n % 60:02d}:00+00:00"}
            for n in range(100, 200)
        ],
        [
            {"id": 7, "type": 1, "date_mod": "2026-10-28T09:00:00+00:00"},
            {"id": 8, "type": 2, "date_mod": "2026-10-28T09:01:00+00:00"},
            {"id": 9, "type": 1, "date_mod": None},
        ],
    ]
    client = make_client(fake)
    changed = await client.changed_since(datetime(2026, 10, 28, 8, 55, tzinfo=UTC))
    assert len(changed) == 101
    assert changed[0].id == 7
    assert 8 not in {c.id for c in changed} and 9 not in {c.id for c in changed}
    searches = [r for r in fake.requests if r.url.path == "/api.php/v2/Assistance/Ticket"]
    assert len(searches) == 2
    query = parse_qs(searches[0].url.query.decode())
    assert query["filter"] == ["date_mod=gt=2026-10-28 08:55:00"]
    assert query["start"] == ["0"] and query["limit"] == ["100"]
    assert parse_qs(searches[1].url.query.decode())["start"] == ["100"]
    await client.aclose()


async def test_add_followup_returns_the_new_id(fake: FakeGlpi) -> None:
    client = make_client(fake)
    assert await client.add_followup(3, "<p>Done</p>", private=False) == 77
    posted = [r for r in fake.requests if r.method == "POST" and "Followup" in r.url.path][0]
    assert json.loads(posted.content) == {"content": "<p>Done</p>", "is_private": False}
    fake.followup_answer = (
        201,
        None,
        {"Location": "/api.php/v2/Assistance/Ticket/3/Timeline/Followup/78"},
    )
    assert await client.add_followup(3, "<p>Done</p>", private=True) == 78
    fake.followup_answer = (201, {}, {})
    with pytest.raises(ItsmRejected):
        await client.add_followup(3, "<p>Done</p>", private=True)
    await client.aclose()


async def test_set_external_id(fake: FakeGlpi) -> None:
    client = make_client(fake)
    await client.set_external_id(3, "INC00002001")
    patched = [r for r in fake.requests if r.method == "PATCH"][0]
    assert json.loads(patched.content) == {"external_id": "INC00002001"}
    await client.aclose()


async def test_find_ticket_id_needs_an_exact_match(fake: FakeGlpi) -> None:
    client = make_client(fake)
    assert await client.find_ticket_id("INC00001234") == 3
    assert await client.find_ticket_id("INC00009999") is None
    await client.aclose()
