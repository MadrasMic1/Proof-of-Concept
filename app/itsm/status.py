# app/itsm/status.py
"""Health of the GLPI sync (Stage 2, Phase 20). Read-only.

collect_status() answers "do RIC and GLPI agree, and if not, what to do?" from
the database alone, so it is fast and works while GLPI is down:

    reconcile  the 15-minute job: watermark, last success, failures in a row
    inbox      rows by status; oldest due row; rows stuck in 'processing';
               failed rows and recent conflicts (listed as problems)
    webhooks   last delivery; GLPI-to-RIC delay over 24 h (from the stored events)
    links      stores linked to GLPI; active incidents without a GLPI ticket
    journal    GLPI entries still waiting for the LLM worker

live_checks() adds what needs GLPI itself (scripts.itsm_status --live): both API
logins and the clock drift, measured from the HTTP Date header of one GLPI answer.

Levels: OK, INFO, WARN, FAIL. The verdict is the worst level (INFO counts as OK).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.itsm.worker import RECONCILE_JOB
from app.models import Incident, IncidentJournal, ItsmInbox, ItsmSyncState, Store
from app.models.values import (
    ACTIVE_INCIDENT_STATES,
    InboxSource,
    InboxStatus,
    JournalProcessStatus,
    JournalSource,
)

OK, INFO, WARN, FAIL = "OK", "INFO", "WARN", "FAIL"
_RANK = {OK: 0, INFO: 0, WARN: 1, FAIL: 2}
STUCK_AFTER = timedelta(minutes=30)
CONFLICT_WINDOW = timedelta(days=7)
WEBHOOK_WINDOW = timedelta(hours=24)
MAX_WEBHOOK_ROWS = 500
MAX_PROBLEMS = 10
MAX_UNLINKED = 10


@dataclass(frozen=True)
class Check:
    area: str
    level: str
    message: str


@dataclass(frozen=True)
class ReconcileInfo:
    watermark: datetime | None
    next_run_at: datetime
    last_started_at: datetime | None
    last_success_at: datetime | None
    consecutive_failures: int
    last_error: str | None
    details: dict[str, Any]


@dataclass(frozen=True)
class InboxInfo:
    by_status: dict[str, int]
    by_source: dict[str, int]
    due: int
    waiting_retry: int
    oldest_due_received_at: datetime | None
    stuck_processing: int


@dataclass(frozen=True)
class ProblemRow:
    inbox_id: int
    status: str
    glpi_ticket_id: int
    incident_number: str | None
    source: str
    attempts: int
    last_error: str | None
    processed_at: datetime | None


@dataclass(frozen=True)
class WebhookInfo:
    last_received_at: datetime | None
    deliveries_24h: int
    delay_median_seconds: float | None
    delay_max_seconds: float | None


@dataclass(frozen=True)
class LinkInfo:
    stores: int
    stores_linked: int
    active_incidents_not_linked: int
    examples: list[str]


@dataclass(frozen=True)
class JournalInfo:
    glpi_pending: int
    glpi_failed: int


@dataclass
class StatusReport:
    checked_at: datetime
    checks: list[Check] = field(default_factory=list)
    reconcile: ReconcileInfo | None = None
    inbox: InboxInfo | None = None
    problems: list[ProblemRow] = field(default_factory=list)
    webhooks: WebhookInfo | None = None
    links: LinkInfo | None = None
    journal: JournalInfo | None = None

    @property
    def verdict(self) -> str:
        worst = max((_RANK[check.level] for check in self.checks), default=0)
        return {0: OK, 1: WARN, 2: FAIL}[worst]

    def add(self, area: str, level: str, message: str) -> None:
        self.checks.append(Check(area, level, message))


def _ago(now: datetime, moment: datetime) -> str:
    seconds = max(int((now - moment).total_seconds()), 0)
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    return f"{seconds // 3600} h ago"


# --- parts -----------------------------------------------------------------------------


async def _reconcile(
    session: AsyncSession, report: StatusReport, now: datetime, settings: Settings
) -> None:
    state = await session.get(ItsmSyncState, RECONCILE_JOB)
    if state is None:
        report.add(
            "reconcile", FAIL, "never set up: run scripts.itsm_link, then start scripts.worker"
        )
        return
    report.reconcile = ReconcileInfo(
        watermark=state.watermark,
        next_run_at=state.next_run_at,
        last_started_at=state.last_started_at,
        last_success_at=state.last_success_at,
        consecutive_failures=state.consecutive_failures,
        last_error=state.last_error,
        details=dict(state.details or {}),
    )
    if state.watermark is None:
        report.add(
            "reconcile",
            FAIL,
            "no watermark: run scripts.itsm_link (or scripts.itsm_resync --start-watermark)",
        )
        return
    limit = settings.itsm_status_max_reconcile_failures
    if state.consecutive_failures >= limit:
        report.add(
            "reconcile",
            FAIL,
            f"{state.consecutive_failures} failed runs in a row: {state.last_error}",
        )
        return
    stale_after = timedelta(seconds=2 * settings.itsm_reconcile_interval_seconds)
    if state.last_success_at is None or now - state.last_success_at > stale_after:
        when = _ago(now, state.last_success_at) if state.last_success_at else "never"
        report.add("reconcile", WARN, f"last successful run {when}: is scripts.worker running?")
        return
    if state.consecutive_failures:
        report.add(
            "reconcile",
            WARN,
            f"last run failed ({state.consecutive_failures} in a row): {state.last_error}",
        )
        return
    report.add(
        "reconcile",
        OK,
        f"last success {_ago(now, state.last_success_at)}, watermark {state.watermark.isoformat()}",
    )


async def _inbox(
    session: AsyncSession, report: StatusReport, now: datetime, settings: Settings
) -> None:
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    rows = await session.execute(
        select(ItsmInbox.status, ItsmInbox.source, func.count()).group_by(
            ItsmInbox.status, ItsmInbox.source
        )
    )
    for status, source, count in rows.all():
        by_status[status] = by_status.get(status, 0) + count
        if status == InboxStatus.PENDING:
            by_source[source] = by_source.get(source, 0) + count
    pending = ItsmInbox.status == InboxStatus.PENDING
    due, oldest = (
        await session.execute(
            select(func.count(), func.min(ItsmInbox.received_at)).where(
                pending, ItsmInbox.available_at <= now
            )
        )
    ).one()
    waiting = await session.scalar(
        select(func.count()).select_from(ItsmInbox).where(pending, ItsmInbox.available_at > now)
    )
    stuck = await session.scalar(
        select(func.count())
        .select_from(ItsmInbox)
        .where(
            ItsmInbox.status == InboxStatus.PROCESSING,
            ItsmInbox.updated_at < now - STUCK_AFTER,
        )
    )
    report.inbox = InboxInfo(by_status, by_source, due or 0, waiting or 0, oldest, stuck or 0)

    max_age = timedelta(minutes=settings.itsm_status_max_inbox_age_minutes)
    if oldest is not None and now - oldest > max_age:
        report.add(
            "inbox",
            WARN,
            f"{due} row(s) due, the oldest received {_ago(now, oldest)}: "
            "is scripts.worker running? Is GLPI up?",
        )
    else:
        report.add("inbox", OK, f"{due or 0} row(s) due, {waiting or 0} waiting for a retry")
    if stuck:
        report.add(
            "inbox", WARN, f"{stuck} row(s) 'processing' for over 30 min: restart scripts.worker"
        )
    failed = by_status.get(InboxStatus.FAILED, 0)
    if failed:
        report.add(
            "inbox",
            WARN,
            f"{failed} failed row(s): fix the cause, then scripts.itsm_resync --retry-failed",
        )


async def _problems(session: AsyncSession, report: StatusReport, now: datetime) -> None:
    columns = (
        ItsmInbox.id,
        ItsmInbox.status,
        ItsmInbox.glpi_ticket_id,
        Incident.number,
        ItsmInbox.source,
        ItsmInbox.attempts,
        ItsmInbox.last_error,
        ItsmInbox.processed_at,
    )
    base = select(*columns).outerjoin(Incident, Incident.glpi_ticket_id == ItsmInbox.glpi_ticket_id)
    failed = (
        await session.execute(
            base.where(ItsmInbox.status == InboxStatus.FAILED)
            .order_by(ItsmInbox.id.desc())
            .limit(MAX_PROBLEMS)
        )
    ).all()
    conflicts = (
        await session.execute(
            base.where(
                ItsmInbox.status == InboxStatus.CONFLICT,
                ItsmInbox.processed_at >= now - CONFLICT_WINDOW,
            )
            .order_by(ItsmInbox.id.desc())
            .limit(MAX_PROBLEMS)
        )
    ).all()
    report.problems = [ProblemRow(*row) for row in (*failed, *conflicts)]
    if conflicts:
        report.add(
            "inbox",
            WARN,
            f"{len(conflicts)} conflict(s) in the last 7 days (GLPI reopened or changed a "
            "finished incident): review them (scripts.itsm_resync --list-conflicts)",
        )


def _delay(event: dict[str, Any]) -> float | None:
    try:
        sent = datetime.fromisoformat(str(event["glpi_time"]))
        received = datetime.fromisoformat(str(event["received_at"]))
    except (KeyError, TypeError, ValueError):
        return None
    return (received - sent).total_seconds()


async def _webhooks(session: AsyncSession, report: StatusReport, now: datetime) -> None:
    webhook = ItsmInbox.source == InboxSource.WEBHOOK
    last = await session.scalar(select(func.max(ItsmInbox.received_at)).where(webhook))
    event_lists = (
        await session.scalars(
            select(ItsmInbox.events)
            .where(webhook, ItsmInbox.received_at >= now - WEBHOOK_WINDOW)
            .order_by(ItsmInbox.id.desc())
            .limit(MAX_WEBHOOK_ROWS)
        )
    ).all()
    delays = [
        delay
        for events in event_lists
        for event in events or []
        if isinstance(event, dict) and (delay := _delay(event)) is not None
    ]
    report.webhooks = WebhookInfo(
        last_received_at=last,
        deliveries_24h=len(delays),
        delay_median_seconds=round(statistics.median(delays), 1) if delays else None,
        delay_max_seconds=round(max(delays), 1) if delays else None,
    )
    if last is None:
        report.add("webhooks", INFO, "no webhook received yet (scripts.itsm_webhooks --status)")
    elif delays:
        report.add(
            "webhooks",
            INFO,
            f"last delivery {_ago(now, last)}; last 24 h: {len(delays)} deliveries, "
            f"delay median {report.webhooks.delay_median_seconds} s, "
            f"max {report.webhooks.delay_max_seconds} s",
        )
    else:
        report.add("webhooks", INFO, f"last delivery {_ago(now, last)}; none in the last 24 h")


async def _links(session: AsyncSession, report: StatusReport) -> None:
    stores, linked = (
        await session.execute(select(func.count(), func.count(Store.glpi_location_id)))
    ).one()
    unlinked_query = (
        select(Incident.number)
        .join(Store, Store.id == Incident.store_id)
        .where(
            Store.glpi_location_id.is_not(None),
            Incident.glpi_ticket_id.is_(None),
            Incident.state.in_(ACTIVE_INCIDENT_STATES),
        )
    )
    count = await session.scalar(select(func.count()).select_from(unlinked_query.subquery()))
    examples = (
        await session.scalars(unlinked_query.order_by(Incident.number).limit(MAX_UNLINKED))
    ).all()
    report.links = LinkInfo(stores or 0, linked or 0, count or 0, list(examples))
    if not linked:
        report.add("links", FAIL, "no store is linked to a GLPI location: run scripts.itsm_link")
    else:
        report.add("links", OK, f"{linked} of {stores} stores linked to GLPI locations")
    if count:
        report.add(
            "links",
            INFO,
            f"{count} active incident(s) at linked stores have no GLPI ticket (RIC only; "
            "run scripts.itsm_link if they exist in GLPI)",
        )


async def _journal(session: AsyncSession, report: StatusReport) -> None:
    rows = dict(
        (
            await session.execute(
                select(IncidentJournal.process_status, func.count())
                .where(
                    IncidentJournal.source == JournalSource.GLPI,
                    IncidentJournal.process_status.in_(
                        (JournalProcessStatus.PENDING, JournalProcessStatus.FAILED)
                    ),
                )
                .group_by(IncidentJournal.process_status)
            )
        ).all()
    )
    pending = rows.get(JournalProcessStatus.PENDING, 0)
    failed = rows.get(JournalProcessStatus.FAILED, 0)
    report.journal = JournalInfo(pending, failed)
    report.add(
        "journal", INFO, f"GLPI entries waiting for the LLM worker: {pending}; failed: {failed}"
    )


async def collect_status(
    session: AsyncSession, *, now: datetime, settings: Settings
) -> StatusReport:
    report = StatusReport(checked_at=now)
    await _reconcile(session, report, now, settings)
    await _inbox(session, report, now, settings)
    await _problems(session, report, now)
    await _webhooks(session, report, now)
    await _links(session, report)
    await _journal(session, report)
    return report


# --- live (needs GLPI) -----------------------------------------------------------------


async def glpi_server_time(
    url: str,
    *,
    timeout: float = 10.0,  # noqa: ASYNC109
    transport: httpx.AsyncBaseTransport | None = None,
    wall_clock: Callable[[], float] = time.time,
) -> tuple[datetime, float]:
    """(GLPI server time, drift in seconds = GLPI - RIC) from the Date header of one
    answer of url. Any HTTP status will do (the V1 API root answers 400). The Date
    header has one-second resolution, so a drift under about 1 s is noise."""
    async with httpx.AsyncClient(timeout=timeout, transport=transport) as client:
        sent = wall_clock()
        response = await client.get(url)
        received = wall_clock()
    header = response.headers.get("Date")
    if not header:
        raise ValueError("GLPI's answer has no Date header")
    glpi_time = parsedate_to_datetime(header).astimezone(UTC)
    midpoint = (sent + received) / 2
    return glpi_time, round(glpi_time.timestamp() - midpoint, 1)


async def live_checks(
    client: Any,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[Check]:
    """GLPI logins (V2 token, V1 session) and clock drift. Never raises."""
    checks: list[Check] = []
    try:
        await client.check_connection()
        who = settings.glpi_api_username
        checks.append(Check("glpi", OK, f"API V2 token and API V1 session as {who}"))
    except Exception as exc:
        checks.append(Check("glpi", FAIL, f"login failed: {exc.__class__.__name__}: {exc}"))
    if not settings.glpi_v1_url:
        checks.append(Check("clock", FAIL, "GLPI_V1_URL is not set"))
        return checks
    try:
        _, drift = await glpi_server_time(
            settings.glpi_v1_url,
            timeout=settings.glpi_timeout_seconds,
            transport=transport,
        )
    except Exception as exc:
        checks.append(Check("clock", FAIL, f"GLPI time not readable: {exc.__class__.__name__}"))
        return checks
    level = OK
    if abs(drift) >= settings.itsm_clock_drift_fail_seconds:
        level = FAIL
    elif abs(drift) >= settings.itsm_clock_drift_warn_seconds:
        level = WARN
    advice = "" if level == OK else " (restart Docker Desktop to resync the VM clock)"
    checks.append(Check("clock", level, f"GLPI clock minus RIC clock: {drift:+.1f} s{advice}"))
    return checks
