# app/itsm/servicenow_client.py
"""Async ServiceNow Table API client supporting OAuth 2.0 client_credentials & Basic Auth.

Combines:
- ServiceNow Zurich+ OAuth 2.0 client_credentials (/oauth_token.do) with token caching & 401 retry
- Optional Basic Auth fallback for local VM simulators
- Idempotent create_incident() via find_incident_by_correlation_id()
- check_connection(), transport injection, and call counter for pytest MockTransport tests
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from app.itsm.errors import ItsmConfigError, ItsmNotFound, ItsmRejected, ItsmUnavailable

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal"})
_ERROR_EXCERPT = 300


@dataclass(frozen=True)
class ServiceNowConfig:
    instance_url: str
    client_id: str | None = None
    client_secret: SecretStr | None = None
    username: str | None = None
    password: SecretStr | None = None
    environment: str = "prod"
    timeout_seconds: float = 30.0
    verify_tls: bool = True
    default_assignment_group: str | None = None

    @property
    def uses_oauth(self) -> bool:
        return bool(
            self.client_id
            and self.client_secret
            and self.client_secret.get_secret_value().strip()
        )

    def __post_init__(self) -> None:
        parsed = urlparse(self.instance_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ItsmConfigError("SERVICENOW_INSTANCE_URL must be a valid http(s) URL")
        if self.environment == "prod" and parsed.scheme != "https":
            raise ItsmConfigError("SERVICENOW_INSTANCE_URL must use https:// in prod")
        if parsed.scheme == "http" and (parsed.hostname or "") not in _LOCAL_HOSTS:
            raise ItsmConfigError(
                "Plain http:// SERVICENOW_INSTANCE_URL is only allowed for localhost/host.docker.internal"
            )
        has_basic = bool(
            self.username and self.password and self.password.get_secret_value().strip()
        )
        if not self.uses_oauth and not has_basic:
            raise ItsmConfigError(
                "ServiceNow requires SERVICENOW_CLIENT_ID + SERVICENOW_CLIENT_SECRET (OAuth 2.0) "
                "or SERVICENOW_USERNAME + SERVICENOW_PASSWORD (Basic Auth)"
            )


@dataclass(frozen=True)
class ServiceNowIncident:
    sys_id: str
    number: str
    state: str
    short_description: str
    url: str


@dataclass(frozen=True)
class ServiceNowPushResult:
    inc_number: str
    sys_id: str
    url: str
    warnings: list[str] = field(default_factory=list)


class ServiceNowClient:
    """Async client for the ServiceNow Table API (/api/now/table/incident)."""

    def __init__(
        self,
        config: ServiceNowConfig,
        *,
        http: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config
        self._owns_http = http is None
        self._base = config.instance_url.rstrip("/")
        self.calls = 0
        self._token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

        auth = (
            None
            if config.uses_oauth
            else (
                config.username or "",
                config.password.get_secret_value() if config.password else "",
            )
        )
        self._http = http or httpx.AsyncClient(
            base_url=self._base,
            timeout=config.timeout_seconds,
            verify=config.verify_tls,
            auth=auth,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    def _incident_url(self, sys_id: str) -> str:
        return f"{self._base}/nav_to.do?uri=incident.do?sys_id={sys_id}"

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        async with self._token_lock:
            now = time.monotonic()
            if not force_refresh and self._token and now < self._token_expires_at:
                return self._token

            assert self._config.client_id and self._config.client_secret
            self.calls += 1
            try:
                resp = await self._http.post(
                    "/oauth_token.do",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._config.client_id,
                        "client_secret": self._config.client_secret.get_secret_value(),
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.TimeoutException as exc:
                raise ItsmUnavailable(
                    f"ServiceNow OAuth: no answer within {self._config.timeout_seconds:g} s"
                ) from exc
            except httpx.HTTPError as exc:
                raise ItsmUnavailable("ServiceNow OAuth: could not connect to ServiceNow") from exc

            payload = self._json(self._check(resp, "ServiceNow OAuth"), "ServiceNow OAuth")
            token = str(payload.get("access_token") or "").strip()
            if not token:
                raise ItsmRejected("ServiceNow OAuth response missing access_token")

            expires_in = int(payload.get("expires_in") or 1800)
            self._token = token
            self._token_expires_at = now + max(30, expires_in - 60)
            return token

    async def _send(self, method: str, path: str, *, what: str, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}) or {})
        if self._config.uses_oauth:
            headers["Authorization"] = f"Bearer {await self._get_access_token()}"

        self.calls += 1
        try:
            response = await self._http.request(method, path, headers=headers, **kwargs)
        except httpx.TimeoutException as exc:
            raise ItsmUnavailable(
                f"{what}: no answer within {self._config.timeout_seconds:g} s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ItsmUnavailable(f"{what}: could not connect to ServiceNow") from exc

        if response.status_code == 401 and self._config.uses_oauth:
            headers["Authorization"] = f"Bearer {await self._get_access_token(force_refresh=True)}"
            self.calls += 1
            try:
                response = await self._http.request(method, path, headers=headers, **kwargs)
            except httpx.TimeoutException as exc:
                raise ItsmUnavailable(
                    f"{what}: no answer within {self._config.timeout_seconds:g} s"
                ) from exc
            except httpx.HTTPError as exc:
                raise ItsmUnavailable(f"{what}: could not connect to ServiceNow") from exc

        return response

    @staticmethod
    def _check(response: httpx.Response, what: str) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        detail = response.text[:_ERROR_EXCERPT].replace("\n", " ").strip()
        message = f"{what}: {detail}" if detail else f"{what}: HTTP {status}"
        if status == 404:
            raise ItsmNotFound(message, status=status)
        if status == 429 or status >= 500:
            raise ItsmUnavailable(message, status=status)
        raise ItsmRejected(message, status=status)

    @staticmethod
    def _json(response: httpx.Response, what: str) -> dict[str, Any]:
        if not response.content.strip():
            return {}
        try:
            payload = response.json()
        except ValueError as exc:
            raise ItsmRejected(
                f"{what}: the answer is not JSON", status=response.status_code
            ) from exc
        return payload if isinstance(payload, dict) else {}

    async def check_connection(self) -> None:
        response = await self._send(
            "GET",
            "/api/now/table/incident",
            what="ServiceNow incident table",
            params={"sysparm_limit": 1, "sysparm_fields": "sys_id"},
        )
        self._check(response, "ServiceNow incident table")

    async def find_incident_by_correlation_id(
        self, correlation_id: str
    ) -> ServiceNowIncident | None:
        """Find the incident by RIC's correlation id, the supported idempotency key."""
        return await self._find_one(correlation_id=correlation_id)

    async def _find_one(
        self,
        *,
        correlation_id: str | None = None,
        inc_number: str | None = None,
    ) -> ServiceNowIncident | None:
        query = (
            f"correlation_id={correlation_id}"
            if correlation_id
            else f"number={inc_number}"
            if inc_number
            else None
        )
        if query is None:
            return None

        response = await self._send(
            "GET",
            "/api/now/table/incident",
            what="ServiceNow incident query",
            params={
                "sysparm_query": query,
                "sysparm_limit": 1,
                "sysparm_fields": "sys_id,number,state,short_description",
            },
        )
        payload = self._json(
            self._check(response, "ServiceNow incident query"),
            "ServiceNow incident query",
        )
        results = payload.get("result") or []
        if not results:
            return None
        row = results[0] if isinstance(results[0], dict) else {}
        sys_id = str(row.get("sys_id") or "").strip()
        number = str(row.get("number") or inc_number or "").strip()
        if not sys_id or not number:
            raise ItsmRejected("ServiceNow query response missing sys_id or incident number")
        return ServiceNowIncident(
            sys_id=sys_id,
            number=number,
            state=str(row.get("state") or ""),
            short_description=str(row.get("short_description") or ""),
            url=self._incident_url(sys_id),
        )

    async def create_incident(
        self,
        *,
        short_description: str,
        description: str,
        impact: int = 2,
        urgency: int = 2,
        correlation_id: str,
        store_code: str,
        caller_id: str,
        cmdb_ci: str | None = None,
        assignment_group: str | None = None,
        dynatrace_problem_id: str | None = None,
    ) -> ServiceNowIncident:
        existing = await self.find_incident_by_correlation_id(correlation_id)
        if existing is not None:
            return existing

        body_lines = [
            description.strip(),
            "",
            f"Store: {store_code}",
            f"RIC Reference: {correlation_id}",
        ]
        if dynatrace_problem_id:
            body_lines.append(f"Dynatrace Problem ID: {dynatrace_problem_id}")

        payload: dict[str, Any] = {
            "short_description": short_description.strip()[:160],
            "description": "\n".join(body_lines)[:4000],
            "impact": str(max(1, min(int(impact), 3))),
            "urgency": str(max(1, min(int(urgency), 3))),
            "correlation_id": correlation_id,
            "correlation_display": "RIC-Backend",
            "caller_id": caller_id,
            "category": "hardware",
        }
        if cmdb_ci:
            payload["cmdb_ci"] = cmdb_ci
        group = assignment_group or self._config.default_assignment_group
        if group:
            payload["assignment_group"] = group

        response = await self._send(
            "POST",
            "/api/now/table/incident",
            what="ServiceNow incident creation",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        result = self._json(
            self._check(response, "ServiceNow incident creation"),
            "ServiceNow incident creation",
        ).get("result", {})
        if not isinstance(result, dict):
            raise ItsmRejected("ServiceNow incident creation returned an unexpected answer")

        sys_id = str(result.get("sys_id") or "").strip()
        number = str(result.get("number") or "").strip()
        if not sys_id or not number:
            raise ItsmRejected("ServiceNow response missing sys_id or incident number")

        return ServiceNowIncident(
            sys_id=sys_id,
            number=number,
            state=str(result.get("state") or "1"),
            short_description=str(result.get("short_description") or short_description),
            url=self._incident_url(sys_id),
        )

    async def get_incident_by_number(self, inc_number: str) -> ServiceNowIncident | None:
        return await self._find_one(inc_number=inc_number)