"""ServiceNowClient against a fake ServiceNow Table API."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.itsm.errors import ItsmRejected, ItsmUnavailable
from app.itsm.servicenow_client import ServiceNowClient, ServiceNowConfig


CONFIG = ServiceNowConfig(
    instance_url="https://snow.test",
    username="ric.api",
    password=SecretStr("secret"),
)


class FakeServiceNow:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status = 200
        self.existing: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status >= 400:
            return httpx.Response(self.status, json={"error": {"message": "forced"}})
        if request.method == "GET":
            return httpx.Response(200, json={"result": self.existing})
        if request.method == "POST":
            body = request.read().decode() if request.content else ""
            assert '"correlation_id"' in body
            return httpx.Response(
                201,
                json={
                    "result": {
                        "sys_id": "abc123",
                        "number": "INC00001234",
                        "state": "1",
                        "short_description": "Network incident",
                    }
                },
            )
        return httpx.Response(405)


def make_client(fake: FakeServiceNow) -> ServiceNowClient:
    transport = httpx.MockTransport(fake)
    return ServiceNowClient(CONFIG, transport=transport)


@pytest.mark.asyncio
async def test_connection_and_lookup() -> None:
    fake = FakeServiceNow()
    fake.existing = [{
        "sys_id": "abc123",
        "number": "INC00001234",
        "state": "2",
        "short_description": "Network incident",
    }]
    client = make_client(fake)

    await client.check_connection()
    found = await client.get_incident_by_number("INC00001234")

    assert found is not None
    assert found.sys_id == "abc123"
    assert found.url == "https://snow.test/nav_to.do?uri=incident.do?sys_id=abc123"
    assert client.calls == 2
    await client.aclose()


@pytest.mark.asyncio
async def test_create_is_idempotent_by_correlation_id() -> None:
    fake = FakeServiceNow()
    fake.existing = [{
        "sys_id": "existing-sys-id",
        "number": "INC00009999",
        "state": "1",
        "short_description": "Existing incident",
    }]
    client = make_client(fake)

    incident = await client.create_incident(
        short_description="Network incident",
        description="The WAN link is down",
        correlation_id="RIC-123",
        store_code="01234",
        caller_id="ric.api",
    )

    assert incident.number == "INC00009999"
    assert len(fake.requests) == 1
    assert fake.requests[0].method == "GET"


@pytest.mark.asyncio
async def test_create_incident_maps_fields() -> None:
    fake = FakeServiceNow()
    client = make_client(fake)

    incident = await client.create_incident(
        short_description="Network incident",
        description="The WAN link is down",
        impact=1,
        urgency=3,
        correlation_id="RIC-123",
        store_code="01234",
        caller_id="ric.api",
        cmdb_ci="cmdb-ci-sys-id",
        assignment_group="Network Operations",
    )

    assert incident.number == "INC00001234"
    post = fake.requests[-1]
    assert post.method == "POST"
    assert client.calls == 2


@pytest.mark.asyncio
async def test_http_failures_use_itsm_error_types() -> None:
    unavailable_fake = FakeServiceNow()
    unavailable_fake.status = 503
    unavailable = make_client(unavailable_fake)
    with pytest.raises(ItsmUnavailable):
        await unavailable.check_connection()

    rejected_fake = FakeServiceNow()
    rejected_fake.status = 400
    rejected = make_client(rejected_fake)
    with pytest.raises(ItsmRejected):
        await rejected.check_connection()
