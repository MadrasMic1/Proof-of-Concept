# tests/test_store_reply_itsm.py
"""Phase 19: a store's reply to an action is posted to the GLPI ticket first."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import delete, func, insert, select

from app.api.deps import get_itsm_client
from app.core.errors import ConflictError
from app.itsm.errors import ItsmNotFound, ItsmRejected, ItsmUnavailable
from app.itsm.responses import (
    ItsmRejectedError,
    ItsmUnavailableError,
    followup_html,
    post_store_reply,
    reply_author,
)
from app.models import Incident, IncidentJournal, StoreAction, StoreActionResponse
from tests.conftest import CommittedData

SITE = "98719"
TICKET = 990719
OPENED = datetime(2026, 10, 30, 3, 0, tzinfo=UTC)


class FakeGlpi:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.posted: list[tuple[int, str, bool]] = []

    async def add_followup(self, ticket_id: int, content_html: str, *, private: bool) -> int:
        if self.error is not None:
            raise self.error
        self.posted.append((ticket_id, content_html, private))
        return 5000 + len(self.posted)


# --- the GLPI part (pure) -------------------------------------------------------------


def test_followup_text() -> None:
    assert reply_author("priya [store] ") == "priya (store)"
    assert reply_author("   ") == "store"
    html = followup_html("01234", "priya.store-manager", 'Store reply to "Fix?": Yes. <ok>')
    assert html == (
        "<p>[Store 01234 - priya.store-manager] Store reply to &quot;Fix?&quot;: "
        "Yes. &lt;ok&gt;</p>"
    )


async def test_post_maps_glpi_errors() -> None:
    glpi = FakeGlpi()
    assert await post_store_reply(glpi, 3, site_code="01234", actor_name="a", text="x") == 5001
    assert glpi.posted[0][0] == 3 and glpi.posted[0][2] is False  # public follow-up
    cases = [
        (ItsmUnavailable("down", status=503), ItsmUnavailableError),
        (ItsmRejected("bad", status=403), ItsmRejectedError),
        (ItsmNotFound("gone", status=404), ConflictError),
    ]
    for error, expected in cases:
        with pytest.raises(expected):
            await post_store_reply(FakeGlpi(error), 3, site_code="01234", actor_name="a", text="x")
    with pytest.raises(ItsmUnavailableError):
        await post_store_reply(None, 3, site_code="01234", actor_name="a", text="x")


# --- the route (committed rows, fake GLPI client) -------------------------------------


@dataclass
class ReplyCase:
    linked_action: uuid.UUID
    local_action: uuid.UUID
    store_id: uuid.UUID


@pytest.fixture
async def case(committed_data: CommittedData) -> AsyncIterator[ReplyCase]:
    store_id = await committed_data.add_store(SITE)
    linked, local = uuid.uuid4(), uuid.uuid4()
    incidents = {
        linked: (uuid.uuid4(), "INC09997191", TICKET),
        local: (uuid.uuid4(), "INC09997192", None),
    }
    async with committed_data.engine.begin() as connection:
        for action_id, (incident_id, number, ticket) in incidents.items():
            await connection.execute(
                insert(Incident).values(
                    id=incident_id,
                    number=number,
                    store_id=store_id,
                    short_description="Printer 02 - Not Printing",
                    category="hardware",
                    contact_type="phone",
                    impact=2,
                    urgency=2,
                    opened_at=OPENED,
                    glpi_ticket_id=ticket,
                )
            )
            await connection.execute(
                insert(StoreAction).values(
                    id=action_id,
                    store_id=store_id,
                    incident_id=incident_id,
                    title="Is Printer 02 printing now?",
                    detail="Please print a test receipt.",
                    severity="warning",
                    expected_response="yes_no",
                    source="seed",
                    created_by="seed",
                )
            )
    yield ReplyCase(linked, local, store_id)
    incident_ids = [value[0] for value in incidents.values()]
    async with committed_data.engine.begin() as connection:
        await connection.execute(
            delete(StoreActionResponse).where(StoreActionResponse.store_id == store_id)
        )
        await connection.execute(delete(StoreAction).where(StoreAction.store_id == store_id))
        await connection.execute(
            delete(IncidentJournal).where(IncidentJournal.incident_id.in_(incident_ids))
        )
        await connection.execute(delete(Incident).where(Incident.id.in_(incident_ids)))


@pytest.fixture
def glpi(app: FastAPI) -> FakeGlpi:
    fake = FakeGlpi()
    app.dependency_overrides[get_itsm_client] = lambda: fake
    return fake


HEADERS = {"X-Site-Id": SITE, "X-User": "priya.store-manager"}


def url(action_id: uuid.UUID) -> str:
    return f"/api/v1/actions/{action_id}/responses"


async def stored(committed_data: CommittedData, action_id: uuid.UUID):
    async with committed_data.engine.connect() as connection:
        return (
            await connection.execute(
                select(IncidentJournal.glpi_followup_id, IncidentJournal.value)
                .join(StoreActionResponse, StoreActionResponse.journal_id == IncidentJournal.id)
                .where(StoreActionResponse.action_id == action_id)
            )
        ).all()


async def test_linked_reply_is_posted_once_then_recorded(
    client: AsyncClient, case: ReplyCase, glpi: FakeGlpi, committed_data: CommittedData
) -> None:
    body = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": True}
    first = await client.post(url(case.linked_action), json=body, headers=HEADERS)
    assert first.status_code == 201, first.text
    assert len(glpi.posted) == 1
    ticket, content, private = glpi.posted[0]
    assert (ticket, private) == (TICKET, False)
    assert content.startswith(f"<p>[Store {SITE} - priya.store-manager] Store reply to")
    [(followup_id, value)] = await stored(committed_data, case.linked_action)
    assert followup_id == 5001 and value in content.replace("&quot;", '"')
    retry = await client.post(url(case.linked_action), json=body, headers=HEADERS)
    assert retry.status_code == 200 and retry.json() == first.json()
    assert len(glpi.posted) == 1  # a retry never posts twice
    again = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": False}
    refused = await client.post(url(case.linked_action), json=again, headers=HEADERS)
    assert refused.status_code == 409 and len(glpi.posted) == 1


async def test_invalid_reply_is_never_posted(
    client: AsyncClient, case: ReplyCase, glpi: FakeGlpi
) -> None:
    body = {"clientRequestId": str(uuid.uuid4()), "responseText": "no yes/no"}
    response = await client.post(url(case.linked_action), json=body, headers=HEADERS)
    assert response.status_code == 422 and glpi.posted == []


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (ItsmUnavailable("down", status=503), 503, "itsm_unavailable"),
        (ItsmRejected("bad token", status=401), 502, "itsm_rejected"),
        (ItsmNotFound("gone", status=404), 409, "conflict"),
    ],
)
async def test_glpi_errors_record_nothing(
    client: AsyncClient,
    case: ReplyCase,
    app: FastAPI,
    committed_data: CommittedData,
    error: Exception,
    status: int,
    code: str,
) -> None:
    app.dependency_overrides[get_itsm_client] = lambda: FakeGlpi(error)
    body = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": True}
    response = await client.post(url(case.linked_action), json=body, headers=HEADERS)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    async with committed_data.engine.connect() as connection:
        count = await connection.scalar(
            select(func.count())
            .select_from(StoreActionResponse)
            .where(StoreActionResponse.action_id == case.linked_action)
        )
        action_status = await connection.scalar(
            select(StoreAction.status).where(StoreAction.id == case.linked_action)
        )
    assert (count, action_status) == (0, "open")


async def test_without_glpi_client_linked_is_503_and_unlinked_is_local(
    client: AsyncClient, case: ReplyCase, app: FastAPI, committed_data: CommittedData
) -> None:
    app.dependency_overrides[get_itsm_client] = lambda: None
    body = {"clientRequestId": str(uuid.uuid4()), "answerYesNo": True}
    linked = await client.post(url(case.linked_action), json=body, headers=HEADERS)
    assert linked.status_code == 503
    local = await client.post(url(case.local_action), json=body, headers=HEADERS)
    assert local.status_code == 201, local.text
    [(followup_id, _)] = await stored(committed_data, case.local_action)
    assert followup_id is None
