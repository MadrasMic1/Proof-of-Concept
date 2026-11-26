# app/itsm/glpi_client.py
"""Async client for GLPI 11: the high-level API V2, plus the legacy API V1 where V2
has no equivalent (Phase 12 findings).

    V2 (OAuth2 password grant)   ticket, team, timeline (follow-ups and solutions),
                                 changed-since search, add follow-up, write external_id
    V1 (session token)           time_to_resolve (ETA), linked items (CI), pending
                                 reason, category parent, ticket search by number

One client per process (API or worker): it holds one HTTP connection pool, the
V2 access token and the V1 session. Tokens are renewed when they expire or when
GLPI answers 401 (once per call). The client never retries anything else: the
caller decides (ItsmUnavailable = retry later, ItsmRejected = fix the cause).
Tokens and passwords never appear in errors or logs.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.itsm.errors import ItsmNotFound, ItsmRejected, ItsmUnavailable
from app.itsm.snapshot import (
    ChangedTicket,
    FollowupSnapshot,
    GlpiAsset,
    GlpiNamed,
    GlpiTicketRef,
    LinkedItem,
    SolutionSnapshot,
    TeamMember,
    TicketSnapshot,
)
from app.itsm.timeparse import parse_glpi_time, to_glpi_text

logger = logging.getLogger("app.itsm")

GLPI_STATUS_PENDING = 4
TICKET_TYPE_INCIDENT = 1
TOKEN_EARLY_REFRESH_SECONDS = 60
DEFAULT_TOKEN_LIFETIME_SECONDS = 3600
CHANGED_PAGE_SIZE = 100
MAX_CHANGED_PAGES = 100
V1_RANGE = "0-999"
V1_PAGE_SIZE = 1000
ASSET_ITEMTYPES = ("NetworkEquipment", "Computer", "Printer", "Peripheral")
_ERROR_EXCERPT = 300
# GLPI actor types (CommonITILActor): 1 requester, 2 assigned, 3 observer.
_ROLE_BY_NUMBER = {1: "requester", 2: "assigned", 3: "observer"}


@dataclass(frozen=True)
class GlpiConfig:
    v2_url: str
    token_url: str
    client_id: str
    client_secret: str
    username: str
    password: str
    v1_url: str
    user_token: str
    app_token: str | None
    timezone: str = "UTC"
    timeout_seconds: float = 30.0


# --- small helpers for GLPI's JSON ---------------------------------------------------


def _ref_id(value: Any) -> int | None:
    """V2 references are {"id": .., "name": ..}; V1 uses plain ids. 0 means none."""
    if isinstance(value, dict):
        value = value.get("id")
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ref_name(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("name")
    if value is None:
        return None
    text = html.unescape(str(value)).strip()
    return text or None


def _int(value: Any, default: int = 0) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _role(value: Any) -> str:
    """'assigned', 'requester' or 'observer' from GLPI's role (text like 'Assigned to',
    or the actor type number)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return _ROLE_BY_NUMBER.get(value, str(value))
    text = str(value or "").strip().lower()
    for prefix, role in (("assign", "assigned"), ("request", "requester"), ("observ", "observer")):
        if text.startswith(prefix):
            return role
    return text


def _items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        return [item for item in payload["data"] if isinstance(item, dict)]
    return []


class GlpiClient:
    def __init__(
        self,
        config: GlpiConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.calls = 0
        self._monotonic = monotonic
        self._http = httpx.AsyncClient(timeout=config.timeout_seconds, transport=transport)
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()
        self._session: str | None = None
        self._session_lock = asyncio.Lock()
        self._v1_cache: dict[tuple[str, int], dict[str, Any]] = {}

    # --- HTTP ------------------------------------------------------------------------

    async def _send(self, method: str, url: str, what: str, **kwargs: Any) -> httpx.Response:
        self.calls += 1
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ItsmUnavailable(
                f"{what}: no answer within {self.config.timeout_seconds:g} s"
            ) from exc
        except httpx.HTTPError as exc:
            raise ItsmUnavailable(f"{what}: could not connect to GLPI") from exc

    @staticmethod
    def _check(response: httpx.Response, what: str) -> httpx.Response:
        status = response.status_code
        if status < 400:
            return response
        detail = response.text[:_ERROR_EXCERPT]
        message = f"{what}: {detail}".rstrip(": ")
        if status == 404 or (status == 400 and "ERROR_ITEM_NOT_FOUND" in detail):
            raise ItsmNotFound(message, status=status)
        if status == 429 or status >= 500:
            raise ItsmUnavailable(message, status=status)
        raise ItsmRejected(message, status=status)

    @staticmethod
    def _json(response: httpx.Response, what: str) -> Any:
        if not response.content.strip():
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise ItsmRejected(
                f"{what}: the answer is not JSON", status=response.status_code
            ) from exc

    # --- API V2 (OAuth2) -------------------------------------------------------------

    async def _v2_token(self, stale: str | None = None) -> str:
        """A valid access token. stale: the token GLPI just refused (401)."""
        async with self._token_lock:
            fresh = self._token is not None and self._monotonic() < self._token_expires_at
            if fresh and self._token != stale:
                return self._token
            form = {
                "grant_type": "password",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "username": self.config.username,
                "password": self.config.password,
                "scope": "api",
            }
            response = await self._send(
                "POST",
                self.config.token_url,
                "GLPI V2 token",
                data=form,
                headers={"Accept": "application/json"},
            )
            if response.status_code in (400, 401):
                raise ItsmRejected(
                    "GLPI V2 token: refused (check GLPI_OAUTH_* and GLPI_API_* settings)",
                    status=response.status_code,
                )
            self._check(response, "GLPI V2 token")
            payload = self._json(response, "GLPI V2 token") or {}
            token = payload.get("access_token") if isinstance(payload, dict) else None
            if not token:
                raise ItsmRejected("GLPI V2 token: no access_token in the answer")
            lifetime = _int(payload.get("expires_in"), DEFAULT_TOKEN_LIFETIME_SECONDS)
            self._token = str(token)
            self._token_expires_at = self._monotonic() + max(
                lifetime - TOKEN_EARLY_REFRESH_SECONDS, 1
            )
            return self._token

    async def _v2(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
    ) -> httpx.Response:
        url = f"{self.config.v2_url.rstrip('/')}/{path.lstrip('/')}"
        what = f"GLPI V2 {method} {path}"
        token = await self._v2_token()
        for attempt in range(2):
            headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
            response = await self._send(
                method, url, what, params=params, json=json, headers=headers
            )
            if response.status_code == 401 and attempt == 0:
                token = await self._v2_token(stale=token)
                continue
            return self._check(response, what)
        raise AssertionError("unreachable")

    async def _v2_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = await self._v2("GET", path, params=params)
        return self._json(response, f"GLPI V2 GET {path}")

    # --- API V1 (session) ------------------------------------------------------------

    def _v1_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.config.app_token:
            headers["App-Token"] = self.config.app_token
        return headers

    async def _v1_session(self, stale: str | None = None) -> str:
        async with self._session_lock:
            if self._session is not None and self._session != stale:
                return self._session
            headers = self._v1_headers()
            headers["Authorization"] = f"user_token {self.config.user_token}"
            url = f"{self.config.v1_url.rstrip('/')}/initSession"
            response = await self._send("GET", url, "GLPI V1 initSession", headers=headers)
            if response.status_code in (400, 401):
                raise ItsmRejected(
                    "GLPI V1 initSession: refused (check GLPI_USER_TOKEN and GLPI_APP_TOKEN)",
                    status=response.status_code,
                )
            self._check(response, "GLPI V1 initSession")
            payload = self._json(response, "GLPI V1 initSession") or {}
            session = payload.get("session_token") if isinstance(payload, dict) else None
            if not session:
                raise ItsmRejected("GLPI V1 initSession: no session_token in the answer")
            self._session = str(session)
            return self._session

    async def _v1_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.config.v1_url.rstrip('/')}/{path.lstrip('/')}"
        what = f"GLPI V1 GET {path}"
        session = await self._v1_session()
        for attempt in range(2):
            headers = self._v1_headers()
            headers["Session-Token"] = session
            response = await self._send("GET", url, what, params=params, headers=headers)
            if response.status_code == 401 and attempt == 0:
                session = await self._v1_session(stale=session)
                continue
            return self._json(self._check(response, what), what)
        raise AssertionError("unreachable")

    async def _v1_item(self, itemtype: str, item_id: int) -> dict[str, Any]:
        """A V1 item, cached for the life of the client (reference data only)."""
        key = (itemtype, item_id)
        if key not in self._v1_cache:
            payload = await self._v1_json(f"{itemtype}/{item_id}")
            self._v1_cache[key] = payload if isinstance(payload, dict) else {}
        return self._v1_cache[key]

    # --- public operations -----------------------------------------------------------

    async def check_connection(self) -> None:
        """Log in to both APIs (used by check_env and itsm_check)."""
        await self._v2_token()
        await self._v1_session()

    async def get_v2_json(self, path: str) -> Any:
        """A raw V2 GET (diagnostics only)."""
        return await self._v2_json(path)

    async def find_ticket_id(self, number: str) -> int | None:
        """The GLPI ticket whose external id is this RIC number, or None."""
        payload = await self._v1_json(
            "Ticket", {"searchText[externalid]": number, "range": V1_RANGE}
        )
        matches = [
            _int(item.get("id"))
            for item in _items(payload)
            if html.unescape(str(item.get("externalid") or "")).strip() == number
        ]
        if len(matches) > 1:
            logger.warning(
                "several GLPI tickets have the same RIC number", extra={"number": number}
            )
        return matches[0] if matches else None

    async def changed_since(self, after: datetime) -> list[ChangedTicket]:
        """Incident tickets whose date_mod is later than after (aware datetime), oldest first."""
        text = to_glpi_text(after, self.tz)
        changed: dict[int, ChangedTicket] = {}
        for page in range(MAX_CHANGED_PAGES):
            params = {
                "filter": f"date_mod=gt={text}",
                "start": page * CHANGED_PAGE_SIZE,
                "limit": CHANGED_PAGE_SIZE,
            }
            items = _items(await self._v2_json("Assistance/Ticket", params))
            for item in items:
                ticket_id = _ref_id(item.get("id"))
                if ticket_id is None:
                    continue
                if "type" in item and _int(item.get("type")) != TICKET_TYPE_INCIDENT:
                    continue
                date_mod = parse_glpi_time(item.get("date_mod"), self.tz)
                if date_mod is not None:
                    changed[ticket_id] = ChangedTicket(id=ticket_id, date_mod=date_mod)
            if len(items) < CHANGED_PAGE_SIZE:
                break
        else:
            logger.warning("changed_since stopped after the page limit", extra={"after": text})
        return sorted(changed.values(), key=lambda item: (item.date_mod, item.id))

    async def get_ticket_snapshot(self, ticket_id: int) -> TicketSnapshot:
        """Everything RIC needs about one ticket: 4 calls (5 when it is Pending)."""
        ticket = await self._v2_json(f"Assistance/Ticket/{ticket_id}")
        if not isinstance(ticket, dict) or _ref_id(ticket.get("id")) != ticket_id:
            raise ItsmRejected(f"GLPI V2 ticket {ticket_id}: unexpected answer")
        timeline = _items(await self._v2_json(f"Assistance/Ticket/{ticket_id}/Timeline"))
        v1_ticket = await self._v1_json(f"Ticket/{ticket_id}")
        if not isinstance(v1_ticket, dict):
            raise ItsmRejected(f"GLPI V1 ticket {ticket_id}: unexpected answer")
        links = _items(await self._v1_json(f"Ticket/{ticket_id}/Item_Ticket", {"range": V1_RANGE}))

        status = _int(ticket.get("status"))
        date_mod = parse_glpi_time(ticket.get("date_mod"), self.tz)
        if date_mod is None:
            raise ItsmRejected(f"GLPI V2 ticket {ticket_id}: no date_mod")

        pending_reason = None
        if status == GLPI_STATUS_PENDING:
            pending_reason = await self._pending_reason_name(ticket_id)

        category_name = category_parent = None
        category_id = _ref_id(v1_ticket.get("itilcategories_id")) or _ref_id(ticket.get("category"))
        if category_id:
            category = await self._v1_item("ITILCategory", category_id)
            category_name = _ref_name(category.get("name"))
            parent_id = _ref_id(category.get("itilcategories_id"))
            if parent_id:
                category_parent = _ref_name(
                    (await self._v1_item("ITILCategory", parent_id)).get("name")
                )

        team = tuple(
            TeamMember(
                kind=str(member.get("type") or "").strip().lower(),
                role=_role(member.get("role")),
                id=_int(member.get("id")),
                name=_ref_name(member.get("name")) or "",
            )
            for member in ticket.get("team") or []
            if isinstance(member, dict)
        )
        followups: list[FollowupSnapshot] = []
        solutions: list[SolutionSnapshot] = []
        for entry in timeline:
            kind = str(entry.get("type") or "").strip().lower()
            item = entry.get("item")
            if not isinstance(item, dict) or _ref_id(item.get("id")) is None:
                continue
            if kind in ("followup", "itilfollowup"):
                followups.append(
                    FollowupSnapshot(
                        id=_int(item.get("id")),
                        content_html=str(item.get("content") or ""),
                        is_private=item.get("is_private") in (True, 1, "1"),
                        author_id=_ref_id(item.get("user")),
                        author_name=_ref_name(item.get("user")),
                        created_at=parse_glpi_time(
                            item.get("date") or item.get("date_creation"), self.tz
                        ),
                        updated_at=parse_glpi_time(item.get("date_mod"), self.tz),
                    )
                )
            elif kind in ("solution", "itilsolution"):
                solutions.append(
                    SolutionSnapshot(
                        id=_int(item.get("id")),
                        content_html=str(item.get("content") or ""),
                        type_name=_ref_name(item.get("type")),
                        author_name=_ref_name(item.get("user")),
                        created_at=parse_glpi_time(item.get("date_creation"), self.tz),
                    )
                )
        linked = tuple(
            LinkedItem(itemtype=str(link.get("itemtype")), item_id=_int(link.get("items_id")))
            for link in links
            if link.get("itemtype") and _int(link.get("items_id")) > 0
        )
        external_id = str(ticket.get("external_id") or "").strip() or None
        return TicketSnapshot(
            id=ticket_id,
            name=_ref_name(ticket.get("name")) or "",
            content_html=str(ticket.get("content") or ""),
            status=status,
            type=_int(ticket.get("type")),
            urgency=_int(ticket.get("urgency"), 3),
            impact=_int(ticket.get("impact"), 3),
            is_deleted=bool(ticket.get("is_deleted")),
            external_id=external_id,
            location_id=_ref_id(ticket.get("location")),
            location_name=_ref_name(ticket.get("location")),
            request_type_name=_ref_name(ticket.get("request_type")),
            category_name=category_name,
            category_parent_name=category_parent,
            writer_name=_ref_name(ticket.get("user_recipient")),
            opened_at=parse_glpi_time(ticket.get("date"), self.tz),
            date_mod=date_mod,
            date_solve=parse_glpi_time(ticket.get("date_solve"), self.tz),
            date_close=parse_glpi_time(ticket.get("date_close"), self.tz),
            time_to_resolve=parse_glpi_time(v1_ticket.get("time_to_resolve"), self.tz),
            pending_reason_name=pending_reason,
            team=team,
            followups=tuple(sorted(followups, key=lambda f: f.id)),
            solutions=tuple(sorted(solutions, key=lambda s: s.id)),
            linked_items=linked,
        )

    async def _pending_reason_name(self, ticket_id: int) -> str | None:
        payload = await self._v1_json(
            "PendingReason_Item", {"searchText[items_id]": str(ticket_id), "range": V1_RANGE}
        )
        rows = [
            row
            for row in _items(payload)
            if row.get("itemtype") == "Ticket" and _int(row.get("items_id")) == ticket_id
        ]
        if not rows:
            return None
        reason_id = _ref_id(max(rows, key=lambda row: _int(row.get("id"))).get("pendingreasons_id"))
        if reason_id is None:
            return None
        return _ref_name((await self._v1_item("PendingReason", reason_id)).get("name"))

    async def add_followup(self, ticket_id: int, content_html: str, *, private: bool) -> int:
        """Add a follow-up (public = comment, private = work note); returns its GLPI id."""
        path = f"Assistance/Ticket/{ticket_id}/Timeline/Followup"
        response = await self._v2(
            "POST", path, json={"content": content_html, "is_private": private}
        )
        payload = self._json(response, f"GLPI V2 POST {path}")
        followup_id = _ref_id(payload.get("id")) if isinstance(payload, dict) else None
        if followup_id is None:
            location = response.headers.get("Location", "").rstrip("/")
            tail = location.rsplit("/", 1)[-1]
            followup_id = int(tail) if tail.isdigit() else None
        if followup_id is None:
            raise ItsmRejected(f"GLPI V2 POST {path}: no follow-up id in the answer")
        return followup_id

    async def set_external_id(self, ticket_id: int, number: str) -> None:
        """Write the RIC incident number into the ticket's external_id."""
        await self._v2("PATCH", f"Assistance/Ticket/{ticket_id}", json={"external_id": number})

    # --- lists for linking (Phase 15) ----------------------------------------------

    async def _v1_list(self, itemtype: str) -> list[dict[str, Any]]:
        """Every non-deleted item of one V1 itemtype, V1_PAGE_SIZE per call."""
        items: list[dict[str, Any]] = []
        start = 0
        while True:
            params = {"range": f"{start}-{start + V1_PAGE_SIZE - 1}"}
            try:
                page = _items(await self._v1_json(itemtype, params))
            except ItsmRejected as exc:
                # GLPI answers 400 ERROR_RANGE_EXCEED_TOTAL when start is past the end.
                if "ERROR_RANGE_EXCEED_TOTAL" in exc.message:
                    break
                raise
            items.extend(page)
            if len(page) < V1_PAGE_SIZE:
                break
            start += V1_PAGE_SIZE
        return items

    async def _named(self, itemtype: str) -> list[GlpiNamed]:
        result = []
        for item in await self._v1_list(itemtype):
            item_id, name = _ref_id(item.get("id")), _ref_name(item.get("name"))
            if item_id is not None and name is not None:
                result.append(GlpiNamed(id=item_id, name=name))
        return result

    async def list_locations(self) -> list[GlpiNamed]:
        return await self._named("Location")

    async def list_groups(self) -> list[GlpiNamed]:
        return await self._named("Group")

    async def list_assets(self) -> list[GlpiAsset]:
        """Assets of every type RIC links (NetworkEquipment, Computer, Printer, Peripheral)."""
        assets = []
        for itemtype in ASSET_ITEMTYPES:
            for item in await self._v1_list(itemtype):
                item_id, name = _ref_id(item.get("id")), _ref_name(item.get("name"))
                if item_id is None or name is None:
                    continue
                assets.append(
                    GlpiAsset(
                        itemtype=itemtype,
                        id=item_id,
                        name=name,
                        location_id=_ref_id(item.get("locations_id")),
                    )
                )
        return assets

    async def list_tickets(self) -> list[GlpiTicketRef]:
        """Every non-deleted ticket (all types): id, RIC number, location, type, date_mod."""
        tickets = []
        for item in await self._v1_list("Ticket"):
            ticket_id = _ref_id(item.get("id"))
            if ticket_id is None:
                continue
            external_id = html.unescape(str(item.get("externalid") or "")).strip()
            tickets.append(
                GlpiTicketRef(
                    id=ticket_id,
                    external_id=external_id or None,
                    location_id=_ref_id(item.get("locations_id")),
                    type=_int(item.get("type")),
                    date_mod=parse_glpi_time(item.get("date_mod"), self.tz),
                    is_deleted=bool(_int(item.get("is_deleted"))),
                )
            )
        return tickets

    # --- GLPI webhooks (Phase 17) ---------------------------------------------------

    async def _v1_write(self, method: str, path: str, body: dict[str, Any]) -> Any:
        """A V1 POST / PUT with the session token (renewed once on 401)."""
        url = f"{self.config.v1_url.rstrip('/')}/{path.lstrip('/')}"
        what = f"GLPI V1 {method} {path}"
        session = await self._v1_session()
        for attempt in range(2):
            headers = self._v1_headers()
            headers["Session-Token"] = session
            response = await self._send(method, url, what, json=body, headers=headers)
            if response.status_code == 401 and attempt == 0:
                session = await self._v1_session(stale=session)
                continue
            return self._json(self._check(response, what), what)
        raise AssertionError("unreachable")

    async def list_webhooks(self) -> list[dict[str, Any]]:
        """Every GLPI webhook (V1 fields: id, name, itemtype, event, url, is_active, ...)."""
        return await self._v1_list("Webhook")

    async def add_webhook(self, fields: dict[str, Any]) -> int:
        payload = await self._v1_write("POST", "Webhook", {"input": fields})
        entry = payload[0] if isinstance(payload, list) and payload else payload
        webhook_id = _ref_id(entry.get("id")) if isinstance(entry, dict) else None
        if webhook_id is None:
            raise ItsmRejected("GLPI V1 POST Webhook: no id in the answer")
        return webhook_id

    async def update_webhook(self, webhook_id: int, fields: dict[str, Any]) -> None:
        await self._v1_write("PUT", f"Webhook/{webhook_id}", {"input": fields})

    # --- RIC -> GLPI: new tickets (Agentic Phase 8) -------------------------------------

    async def create_ticket(self, fields: dict[str, Any]) -> int:
        """Create one ticket with API V1 (fields as V1 names); returns its GLPI id."""
        payload = await self._v1_write("POST", "Ticket", {"input": fields})
        entry = payload[0] if isinstance(payload, list) and payload else payload
        ticket_id = _ref_id(entry.get("id")) if isinstance(entry, dict) else None
        if ticket_id is None:
            raise ItsmRejected("GLPI V1 POST Ticket: no id in the answer")
        return ticket_id

    async def link_item(self, ticket_id: int, itemtype: str, item_id: int) -> None:
        """Link an asset (the incident's CI) to a ticket."""
        body = {"input": {"tickets_id": ticket_id, "itemtype": itemtype, "items_id": item_id}}
        await self._v1_write("POST", "Item_Ticket", body)

    async def find_category_id(self, category: str, subcategory: str | None) -> int | None:
        """The ITIL category "<category> > <subcategory>", else "<category>", compared
        case-insensitively on GLPI's complete name; None when GLPI has neither. The
        category list is read once per client."""
        categories: dict[str, int] | None = getattr(self, "_categories", None)
        if categories is None:
            categories = {}
            try:
                items = await self._v1_list("ITILCategory")
            except (ItsmRejected, ItsmNotFound):
                items = []
            for item in items:
                item_id = _ref_id(item.get("id"))
                name = _ref_name(item.get("completename")) or _ref_name(item.get("name"))
                if item_id is not None and name:
                    key = " > ".join(part.strip().lower() for part in name.split(">"))
                    categories.setdefault(key, item_id)
            self._categories = categories
        wanted = [category.strip().lower()]
        if subcategory:
            wanted.insert(0, f"{wanted[0]} > {subcategory.strip().lower()}")
        for key in wanted:
            if key in categories:
                return categories[key]
        return None

    async def aclose(self) -> None:
        try:
            if self._session is not None:
                headers = self._v1_headers()
                headers["Session-Token"] = self._session
                url = f"{self.config.v1_url.rstrip('/')}/killSession"
                await self._send("GET", url, "GLPI V1 killSession", headers=headers)
        except ItsmUnavailable:
            pass
        finally:
            self._session = None
            await self._http.aclose()
