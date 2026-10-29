# app/itsm/webhooks.py
"""GLPI webhooks into RIC (Stage 2, Phase 17).

GLPI calls POST /api/v1/integrations/glpi/webhooks/{itemtype}/{event} from its queue
job (about once a minute; 3 tries, one minute apart). The route must answer fast
and do almost nothing, so this module only:

    1. checks the request is from GLPI: HMAC-SHA256(GLPI_WEBHOOK_SECRET,
       raw body + X-GLPI-timestamp) == X-GLPI-signature (layout proven in Phase 4),
    2. checks the timestamp is not older than 24 hours / not more than 5 minutes
       in the future (GLPI stamps a delivery when it QUEUES it: retries arrive late),
    3. finds the GLPI ticket the delivery is about,
    4. records it in itsm_inbox: a new pending row, or merged into the ticket's
       pending row (one sync per burst). The FULL payload is kept with each event.

The sync itself (app/itsm/sync.py) runs later and always reads a fresh snapshot
of the ticket from GLPI, so a payload is never applied directly: it is kept for
audit and troubleshooting. A delivery that can never be used (unknown item type,
not a ticket, no ticket id, not JSON) is answered 202 and only logged, because an
error status would make GLPI retry something that cannot succeed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError
from app.models.values import InboxSource

logger = logging.getLogger("app.itsm.webhooks")

SIGNATURE_HEADER = "X-GLPI-Signature"
TIMESTAMP_HEADER = "X-GLPI-Timestamp"
# Public path of the route (API prefix included): used to build the URL GLPI calls.
WEBHOOK_PATH = "/api/v1/integrations/glpi/webhooks"

# URL path item type -> GLPI itemtype, and the events RIC accepts for it.
ITEMTYPES = {"ticket": "Ticket", "itilfollowup": "ITILFollowup", "itilsolution": "ITILSolution"}
EVENTS = frozenset({"new", "update"})
TICKET_ITEMTYPE = "Ticket"

MERGE_SQL = text(
    """
    INSERT INTO itsm_inbox (glpi_ticket_id, source, events)
    VALUES (:ticket, :source, CAST(:events AS jsonb))
    ON CONFLICT (glpi_ticket_id) WHERE status = 'pending'
    DO UPDATE SET events = itsm_inbox.events || EXCLUDED.events,
                  merged_count = itsm_inbox.merged_count + 1
    RETURNING id, merged_count
    """
)


class WebhookUnauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class WebhookTooLarge(AppError):
    status_code = 413
    code = "payload_too_large"


class WebhookNotConfigured(AppError):
    status_code = 503
    code = "not_configured"


@dataclass(frozen=True)
class Delivery:
    """A signed delivery GLPI made: what fired and when GLPI queued it."""

    itemtype: str  # GLPI itemtype, e.g. ITILFollowup
    event: str  # new / update
    glpi_time: datetime
    payload: Any


@dataclass(frozen=True)
class Recorded:
    accepted: bool
    glpi_ticket_id: int | None = None
    inbox_id: int | None = None
    merged_count: int | None = None
    reason: str | None = None


# --- checks (pure) -----------------------------------------------------------------------


def sign(secret: str, body: bytes, timestamp: str) -> str:
    """The signature GLPI sends: hex HMAC-SHA256 of body + timestamp."""
    return hmac.new(
        secret.encode("utf-8"), body + timestamp.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def check_signature(
    secret: str | None, body: bytes, signature: str | None, timestamp: str | None
) -> None:
    if not secret:
        raise WebhookNotConfigured("GLPI webhooks are not configured (GLPI_WEBHOOK_SECRET).")
    if not signature or not timestamp:
        raise WebhookUnauthorized("The GLPI signature headers are missing.")
    expected = sign(secret, body, timestamp.strip())
    if not hmac.compare_digest(expected, signature.strip().lower()):
        raise WebhookUnauthorized("The GLPI signature is not valid.")


def check_timestamp(
    timestamp: str, now: datetime, *, max_age: timedelta, max_future: timedelta
) -> datetime:
    """The time GLPI queued the delivery (UTC). Too old or in the future: 401."""
    try:
        moment = datetime.fromtimestamp(int(timestamp.strip()), UTC)
    except (ValueError, OverflowError, OSError) as exc:
        raise WebhookUnauthorized("The GLPI timestamp is not valid.") from exc
    if moment < now - max_age:
        raise WebhookUnauthorized("The GLPI delivery is too old.")
    if moment > now + max_future:
        raise WebhookUnauthorized("The GLPI timestamp is in the future.")
    return moment


def _positive_id(value: Any) -> int | None:
    if isinstance(value, dict):
        value = value.get("id")
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def ticket_id_of(itemtype: str, payload: Any) -> tuple[int | None, str | None]:
    """(GLPI ticket id, None) or (None, why the delivery is not usable).

    Ticket: item.id. Follow-up / solution: only when it belongs to a Ticket (GLPI
    sends the same webhooks for Change and Problem follow-ups): parent_item.id,
    else item.items_id.
    """
    if not isinstance(payload, dict):
        return None, "the payload is not a JSON object"
    item = payload.get("item")
    if not isinstance(item, dict):
        return None, "the payload has no item"
    if itemtype == TICKET_ITEMTYPE:
        ticket_id = _positive_id(item.get("id"))
        return (ticket_id, None) if ticket_id else (None, "the ticket has no id")
    parent_type = item.get("itemtype")
    if parent_type not in (None, TICKET_ITEMTYPE):
        return None, f"the {itemtype} belongs to a {parent_type}, not a ticket"
    ticket_id = _positive_id(payload.get("parent_item")) or _positive_id(item.get("items_id"))
    return (ticket_id, None) if ticket_id else (None, f"the {itemtype} has no ticket id")


def webhook_url(base_url: str, itemtype: str, event: str) -> str:
    """The URL GLPI calls for one webhook, e.g. .../webhooks/itilfollowup/new."""
    return f"{base_url.rstrip('/')}{WEBHOOK_PATH}/{itemtype.lower()}/{event}"


# --- recording ----------------------------------------------------------------------------


async def record_delivery(
    session: AsyncSession,
    *,
    path_itemtype: str,
    path_event: str,
    body: bytes,
    glpi_time: datetime,
    now: datetime,
    commit: bool = True,
) -> Recorded:
    """Queue the ticket of one verified delivery (or explain why it is not usable)."""
    itemtype = ITEMTYPES.get(path_itemtype.lower())
    event = path_event.lower()
    label = f"{path_itemtype}/{path_event}"
    if itemtype is None or event not in EVENTS:
        return _ignored(label, f"RIC does not handle {label} webhooks")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _ignored(label, "the body is not JSON")
    ticket_id, why = ticket_id_of(itemtype, payload)
    if ticket_id is None:
        return _ignored(label, why or "no ticket id")
    item = payload.get("item") if isinstance(payload, dict) else None
    entry = {
        "event": f"{itemtype}/{event}",
        "item_id": _positive_id(item.get("id")) if isinstance(item, dict) else None,
        "glpi_time": glpi_time.isoformat(),
        "received_at": now.isoformat(),
        "payload": payload,
    }
    row = (
        await session.execute(
            MERGE_SQL,
            {
                "ticket": ticket_id,
                "source": str(InboxSource.WEBHOOK),
                "events": json.dumps([entry], default=str),
            },
        )
    ).one()
    if commit:
        await session.commit()
    else:
        await session.flush()
    return Recorded(True, ticket_id, int(row.id), int(row.merged_count))


def _ignored(label: str, reason: str) -> Recorded:
    logger.warning("glpi webhook ignored", extra={"webhook": label, "reason": reason})
    return Recorded(False, reason=reason)
