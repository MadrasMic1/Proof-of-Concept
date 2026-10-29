# app/itsm/mapping.py
"""Turn a GLPI TicketSnapshot into RIC values. Pure functions: no I/O.

Names, not RIC ids: the mapping returns GLPI ids (location, group, asset) and
names (technician login). Turning those into RIC rows needs the database and
is done by the sync service.

Value rules (Stage 1 and Phase 12):
    status        1 New -> 1, 2/3 Processing -> 2, 4 Pending -> 3, 5 Solved -> 6,
                  6 Closed -> 7; solved/closed with solution type "Cancelled" -> 8
    impact/urg.   GLPI 5,4 -> 1   3 -> 2   2,1 -> 3   (priority is computed by the database)
    hold reason   pending reason name; Pending without a reason -> awaiting_caller
    category      parent ITIL category -> category, child -> subcategory
    contact type  request source name
    description   ticket text before the "RIC details" block; the block's lines
                  give symptom, business service, caller, opened by and ETA note
    journal       private follow-up -> work_notes, public -> comments;
                  "[Store <site> - <name>] " prefix -> written by the store
Unknown values get a safe default and a warning (returned, never raised).
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser

from app.itsm.snapshot import LinkedItem, SolutionSnapshot, TicketSnapshot
from app.models.values import (
    ActorType,
    ContactType,
    GlpiItemtype,
    HoldReason,
    IncidentCategory,
    IncidentState,
    JournalElement,
)

STATE_BY_STATUS: dict[int, IncidentState] = {
    1: IncidentState.NEW,
    2: IncidentState.IN_PROGRESS,
    3: IncidentState.IN_PROGRESS,
    4: IncidentState.ON_HOLD,
    5: IncidentState.RESOLVED,
    6: IncidentState.CLOSED,
}
CANCELLED_SOLUTION_TYPE = "Cancelled"
LEVEL_BY_GLPI = {5: 1, 4: 1, 3: 2, 2: 3, 1: 3}
CATEGORY_BY_NAME = {
    "Network": IncidentCategory.NETWORK,
    "Hardware": IncidentCategory.HARDWARE,
    "Software": IncidentCategory.SOFTWARE,
    "Inquiry": IncidentCategory.INQUIRY,
}
CONTACT_TYPE_BY_NAME = {
    "Monitoring": ContactType.MONITORING,
    "Phone": ContactType.PHONE,
    "E-Mail": ContactType.EMAIL,
    "Helpdesk": ContactType.SELF_SERVICE,
    "Direct": ContactType.WALK_IN,
}
HOLD_REASON_BY_NAME = {
    "Awaiting caller": HoldReason.AWAITING_CALLER,
    "Awaiting vendor": HoldReason.AWAITING_VENDOR,
    "Awaiting site visit": HoldReason.AWAITING_SITE_VISIT,
    "Awaiting change": HoldReason.AWAITING_CHANGE,
    "Awaiting problem": HoldReason.AWAITING_PROBLEM,
}
DEFAULT_HOLD_REASON = HoldReason.AWAITING_CALLER
DEFAULT_CONTACT_TYPE = ContactType.PHONE
DEFAULT_CATEGORY = IncidentCategory.INQUIRY
DETAILS_HEADING = "RIC details"
DETAIL_FIELDS = {
    "Symptom": "u_symptom",
    "Business service": "business_service",
    "Caller": "caller",
    "Opened by": "opened_by",
    "ETA note": "u_eta_note",
}
INCIDENT_NUMBER = re.compile(r"^INC[0-9]{7,}$")
STORE_PREFIX = re.compile(r"^\[Store (\d{5}) - ([^\]]+)\]\s*")
GLPI_ASSET_TYPES = {str(member) for member in GlpiItemtype}
TICKET_TYPE_INCIDENT = 1
TITLE_FROM_TEXT_LENGTH = 70


# --- text ------------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.parts.append("\n")
        elif tag in _BLOCK_TAGS and self.parts and not self.parts[-1].endswith("\n"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """GLPI rich text (HTML, sometimes entity-encoded) -> plain text, one line per block."""
    if not value:
        return ""
    text = value
    if "<" not in text and ("&lt;" in text or "&#60;" in text):
        text = html.unescape(text)
    parser = _TextExtractor()
    parser.feed(text)
    parser.close()
    joined = "".join(parser.parts).replace("\r\n", "\n").replace("\xa0", " ")
    lines = [line.strip() for line in joined.split("\n")]
    return re.sub(r"\n{2,}", "\n", "\n".join(lines)).strip()


def text_to_html(text: str) -> str:
    """Plain text -> one HTML paragraph (newlines become <br>), for follow-ups RIC writes."""
    return "<p>" + html.escape(text.strip()).replace("\n", "<br>") + "</p>"


def store_prefix(site_code: str, author: str) -> str:
    """The prefix of a follow-up RIC writes for a store, e.g. '[Store 01234 - Ravi] '."""
    return f"[Store {site_code} - {author}] "


def split_store_prefix(text: str) -> tuple[str | None, str | None, str]:
    """'[Store 01234 - Store Manager] Done' -> ('01234', 'Store Manager', 'Done')."""
    match = STORE_PREFIX.match(text)
    if not match:
        return None, None, text
    return match.group(1), match.group(2).strip(), text[match.end() :].strip()


def split_description(text: str) -> tuple[str | None, dict[str, str]]:
    """Ticket text -> (description, {field: value}) from the optional 'RIC details' block."""
    lines = text.split("\n")
    if DETAILS_HEADING not in lines:
        return text.strip() or None, {}
    index = lines.index(DETAILS_HEADING)
    details: dict[str, str] = {}
    for line in lines[index + 1 :]:
        label, sep, value = line.partition(": ")
        if sep and label.strip() in DETAIL_FIELDS and value.strip():
            details[DETAIL_FIELDS[label.strip()]] = value.strip()
    return "\n".join(lines[:index]).strip() or None, details


# --- results ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncidentFields:
    """RIC incident values of one GLPI ticket. GLPI ids are resolved by the sync service."""

    glpi_ticket_id: int
    number: str | None
    is_deleted: bool
    is_incident: bool
    glpi_location_id: int | None
    location_name: str | None
    short_description: str
    description: str | None
    u_symptom: str | None
    business_service: str | None
    category: str
    subcategory: str | None
    contact_type: str
    caller: str | None
    opened_by: str | None
    impact: int
    urgency: int
    state: int
    hold_reason: str | None
    glpi_group_id: int | None
    group_name: str | None
    assigned_to: str | None
    ci: LinkedItem | None
    u_resolution_eta: datetime | None
    u_eta_note: str | None
    opened_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    close_code: str | None
    close_notes: str | None
    itsm_updated_at: datetime
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class JournalFields:
    """One GLPI follow-up as a RIC journal entry."""

    glpi_followup_id: int
    element: str
    value: str
    author_type: str
    created_by: str
    created_at: datetime | None
    updated_at: datetime | None
    own_write: bool
    store_site: str | None


# --- mapping ---------------------------------------------------------------------------


def map_state(status: int, solution: SolutionSnapshot | None) -> int:
    """GLPI status (+ latest solution) -> RIC incident state."""
    state = STATE_BY_STATUS.get(status)
    if state is None:
        raise ValueError(f"unknown GLPI ticket status {status}")
    if state in (IncidentState.RESOLVED, IncidentState.CLOSED) and solution is not None:
        if solution.type_name == CANCELLED_SOLUTION_TYPE:
            return int(IncidentState.CANCELED)
    return int(state)


def map_level(value: int) -> int:
    """GLPI impact/urgency (1-5) -> RIC (1 high, 2 medium, 3 low). Unknown -> 2."""
    return LEVEL_BY_GLPI.get(value, 2)


def latest_solution(snapshot: TicketSnapshot) -> SolutionSnapshot | None:
    if not snapshot.solutions:
        return None
    return max(snapshot.solutions, key=lambda s: (s.created_at or snapshot.date_mod, s.id))


def map_incident(snapshot: TicketSnapshot) -> IncidentFields:
    warnings: list[str] = []
    solution = latest_solution(snapshot)
    state = map_state(snapshot.status, solution)

    description, details = split_description(html_to_text(snapshot.content_html))
    title = snapshot.name.strip() or (description or "")[:TITLE_FROM_TEXT_LENGTH].strip()
    if not title:
        title = f"GLPI ticket {snapshot.id}"
        warnings.append("ticket has no title and no text")

    if snapshot.category_parent_name in CATEGORY_BY_NAME:
        category = CATEGORY_BY_NAME[snapshot.category_parent_name]
        subcategory = snapshot.category_name
    elif snapshot.category_name in CATEGORY_BY_NAME:
        category, subcategory = CATEGORY_BY_NAME[snapshot.category_name], None
    else:
        category, subcategory = DEFAULT_CATEGORY, snapshot.category_name
        warnings.append(f"unknown category {snapshot.category_name!r}: stored as inquiry")

    contact_type = CONTACT_TYPE_BY_NAME.get(snapshot.request_type_name or "")
    if contact_type is None:
        contact_type = DEFAULT_CONTACT_TYPE
        warnings.append(f"unknown request source {snapshot.request_type_name!r}: stored as phone")

    hold_reason = None
    if state == IncidentState.ON_HOLD:
        hold_reason = HOLD_REASON_BY_NAME.get(snapshot.pending_reason_name or "")
        if hold_reason is None:
            hold_reason = DEFAULT_HOLD_REASON
            warnings.append(
                f"pending reason {snapshot.pending_reason_name!r} unknown or missing: "
                "stored as awaiting_caller"
            )

    groups = [m for m in snapshot.team if m.kind == "group" and m.role == "assigned"]
    users = [m for m in snapshot.team if m.kind == "user" and m.role == "assigned"]
    if len(groups) > 1:
        warnings.append(f"{len(groups)} assigned groups: the first is used")
    if len(users) > 1:
        warnings.append(f"{len(users)} assigned technicians: the first is used")

    assets = [item for item in snapshot.linked_items if item.itemtype in GLPI_ASSET_TYPES]
    if len(assets) > 1:
        warnings.append(f"{len(assets)} linked assets: the first is used as the CI")
    if len(assets) < len(snapshot.linked_items):
        warnings.append("linked items that are not assets are ignored")

    opened_at = snapshot.opened_at
    if opened_at is None:
        opened_at = snapshot.date_mod
        warnings.append("ticket has no opening date: date_mod used")

    finished = (IncidentState.RESOLVED, IncidentState.CLOSED, IncidentState.CANCELED)
    resolved_at = closed_at = None
    close_code = close_notes = None
    if state in finished:
        resolved_at = snapshot.date_solve
        closed_at = snapshot.date_close
        if solution is not None:
            close_code = solution.type_name
            close_notes = html_to_text(solution.content_html) or None
        else:
            warnings.append("solved or closed ticket without a solution")
    if state in (IncidentState.RESOLVED, IncidentState.CLOSED) and resolved_at is None:
        resolved_at = closed_at or snapshot.date_mod
        warnings.append("solved ticket without a solve date: close date or date_mod used")
    if state == IncidentState.CLOSED and closed_at is None:
        closed_at = snapshot.date_mod
        warnings.append("closed ticket without a close date: date_mod used")
    if resolved_at is not None and resolved_at < opened_at:
        resolved_at = opened_at
        warnings.append("solve date before opening date: opening date used")

    number = snapshot.external_id if INCIDENT_NUMBER.match(snapshot.external_id or "") else None
    return IncidentFields(
        glpi_ticket_id=snapshot.id,
        number=number,
        is_deleted=snapshot.is_deleted,
        is_incident=snapshot.type == TICKET_TYPE_INCIDENT,
        glpi_location_id=snapshot.location_id,
        location_name=snapshot.location_name,
        short_description=title,
        description=description,
        u_symptom=details.get("u_symptom"),
        business_service=details.get("business_service"),
        category=str(category),
        subcategory=subcategory,
        contact_type=str(contact_type),
        caller=details.get("caller"),
        opened_by=details.get("opened_by") or snapshot.writer_name,
        impact=map_level(snapshot.impact),
        urgency=map_level(snapshot.urgency),
        state=state,
        hold_reason=str(hold_reason) if hold_reason else None,
        glpi_group_id=groups[0].id if groups else None,
        group_name=groups[0].name if groups else None,
        assigned_to=users[0].name if users else None,
        ci=assets[0] if assets else None,
        u_resolution_eta=snapshot.time_to_resolve,
        u_eta_note=details.get("u_eta_note"),
        opened_at=opened_at,
        resolved_at=resolved_at,
        closed_at=closed_at,
        close_code=close_code,
        close_notes=close_notes,
        itsm_updated_at=snapshot.date_mod,
        warnings=tuple(warnings),
    )


def map_journal(snapshot: TicketSnapshot, api_username: str) -> list[JournalFields]:
    """Follow-ups as journal entries, oldest first. Empty follow-ups are left out."""
    entries: list[JournalFields] = []
    ordered = sorted(snapshot.followups, key=lambda f: (f.created_at or snapshot.date_mod, f.id))
    for followup in ordered:
        site, store_author, text = split_store_prefix(html_to_text(followup.content_html))
        if not text:
            continue
        author = followup.author_name or "glpi"
        element = JournalElement.WORK_NOTES if followup.is_private else JournalElement.COMMENTS
        entries.append(
            JournalFields(
                glpi_followup_id=followup.id,
                element=str(element),
                value=text,
                author_type=str(ActorType.STORE_USER if site else ActorType.IT_STAFF),
                created_by=store_author if site and store_author else author,
                created_at=followup.created_at,
                updated_at=followup.updated_at,
                own_write=followup.author_name == api_username,
                store_site=site,
            )
        )
    return entries
