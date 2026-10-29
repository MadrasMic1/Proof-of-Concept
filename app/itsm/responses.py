# app/itsm/responses.py
"""A store's reply to an action, posted to the GLPI ticket (Stage 2, Phase 19).

GLPI is the system of record: the route posts the reply first and records it in RIC
only after GLPI accepted it, with the new follow-up's id. When GLPI's webhook for that
follow-up arrives, the sync finds the id already stored and changes nothing.

The follow-up is public (technicians and the GLPI requester see it) and written by
the integration user, so the store and the person are in the text:
    [Store 01234 - priya.store-manager] Store reply to "...": Yes.
The sync recognises that prefix and maps it back to a store_user journal entry.

GLPI errors become API errors (nothing is recorded in RIC):
    ItsmUnavailable  503 itsm_unavailable  the app may retry with the same clientRequestId
    ItsmRejected     502 itsm_rejected     a GLPI configuration problem (logged)
    ItsmNotFound     409                   the ticket no longer exists in GLPI
"""

from __future__ import annotations

import logging
from typing import Protocol

from app.core.errors import BadGatewayError, ConflictError, ServiceUnavailableError
from app.itsm.errors import ItsmNotFound, ItsmRejected, ItsmUnavailable
from app.itsm.mapping import store_prefix, text_to_html

logger = logging.getLogger("app.itsm.responses")


class ItsmUnavailableError(ServiceUnavailableError):
    code = "itsm_unavailable"


class ItsmRejectedError(BadGatewayError):
    code = "itsm_rejected"


class FollowupClient(Protocol):
    async def add_followup(self, ticket_id: int, content_html: str, *, private: bool) -> int: ...


def reply_author(actor_name: str) -> str:
    """The person in the [Store ... - <author>] prefix (no brackets: they end the prefix)."""
    cleaned = " ".join(actor_name.replace("[", "(").replace("]", ")").split())
    return cleaned or "store"


def followup_html(site_code: str, actor_name: str, text: str) -> str:
    return text_to_html(store_prefix(site_code, reply_author(actor_name)) + text)


async def post_store_reply(
    client: FollowupClient | None,
    glpi_ticket_id: int,
    *,
    site_code: str,
    actor_name: str,
    text: str,
) -> int:
    """Post the reply as a public follow-up; returns its GLPI id."""
    if client is None:
        logger.error("store reply not sent: the GLPI client is not configured")
        raise ItsmUnavailableError(
            "The ITSM connection is not configured; the reply was not saved."
        )
    content = followup_html(site_code, actor_name, text)
    try:
        return await client.add_followup(glpi_ticket_id, content, private=False)
    except ItsmUnavailable as exc:
        logger.warning(
            "store reply not sent: GLPI unavailable",
            extra={"glpiTicketId": glpi_ticket_id, "error": str(exc)},
        )
        raise ItsmUnavailableError(
            "The ITSM did not answer; the reply was not saved. Please try again."
        ) from exc
    except ItsmNotFound as exc:
        raise ConflictError("The incident is no longer available in the ITSM.") from exc
    except ItsmRejected as exc:
        logger.error(
            "store reply refused by GLPI",
            extra={"glpiTicketId": glpi_ticket_id, "error": str(exc)},
        )
        raise ItsmRejectedError("The ITSM refused the reply; it was not saved.") from exc
