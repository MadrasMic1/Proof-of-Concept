# RIC Backend (POC)

FastAPI + PostgreSQL (asyncpg) backend for the RIC POC.

## Prerequisites

- Windows / Azure Virtual Desktop with PowerShell
- PostgreSQL 13+ running locally
- uv: `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
  (on machines that block install scripts: `pip install uv`)

## First-time setup

```powershell
uv python install 3.12                    # if Python 3.12 is not installed
uv sync                                   # create .venv from uv.lock
Copy-Item .env.example .env               # then fill in passwords
uv run python -m scripts.db_bootstrap     # role ric_app + ric_poc + ric_poc_test
uv run python -m scripts.check_env        # all checks must pass
uv run alembic upgrade head               # create the 24 tables (revisions 0001-0004)
uv run python -m scripts.db_status        # revision and table count must be OK
uv run python -m scripts.seed all         # load the POC seed data (network, ITSM, notifications)
uv run pytest                             # tests (use ric_poc_test, never ric_poc)
```

## Run the demo from scratch

```powershell
git clone <repo-url>; cd ric-backend
uv sync
Copy-Item .env.example .env               # set the PostgreSQL passwords and LLM_API_KEY (nvapi-...)
uv run python -m scripts.db_bootstrap     # role ric_app + ric_poc + ric_poc_test
uv run python -m scripts.demo --reset --yes
uv run uvicorn app.main:app --port 8000   # then open http://127.0.0.1:8000/docs
```

`scripts.demo` rebuilds the schema, loads every seed step, lets the worker process the journal
with the configured LLM and ends with `scripts.verify` (every check must pass).

## Running the API

```powershell
uv run uvicorn app.main:app --reload      # http://localhost:8000
```

| URL | Purpose |
|---|---|
| `http://localhost:8000/health` | Liveness: the process is running (never touches the database) |
| `http://localhost:8000/health/ready` | Readiness: 200 when the database answers, 503 when it does not |
| `http://localhost:8000/docs` | Interactive API documentation (when `DOCS_ENABLED=true`) |

### Mobile routes (all need `X-Site-Id`)

| Route | Returns |
|---|---|
| `GET /api/v1/store` | The store: name, timezone, local time, open now |
| `GET /api/v1/network` | Routers and switches (`wiredTiles`), WAN links, wired connections, access points with open incidents |
| `GET /api/v1/devices` | One tile per device type: colour, total, online |
| `GET /api/v1/devices/{deviceType}` | The devices of one type (e.g. `pos`, `rf_gun`), problems first |
| `GET /api/v1/incidents` | The store's active incidents (states 1-3), highest severity first, then newest |
| `GET /api/v1/incidents/{number}` | One incident (any state) with its store-visible comments |
| `GET /api/v1/appointments` | Upcoming visits (scheduled or in progress, not yet over), soonest first |
| `GET /api/v1/alerts` | Active alerts (one per open incident), critical first, newest first |
| `GET /api/v1/actions` | Open actions not yet overdue (soonest due first, `dueByText` in store time), then actions answered in the last 24 hours (`status` `responded`, with `response`) |
| `POST /api/v1/actions/{actionId}/responses` | The store's reply: `clientRequestId` (UUID per tap), `answerYesNo` for yes/no actions, `responseText` (required for free_text). 201 new, 200 repeated request id, 409 answered / closed / expired |
| `GET /api/v1/notifications` | The store's live notifications, newest first (`?limit=`, default 50, max 200); marks them delivered |
| `GET /api/v1/notifications/unread-count` | `{"unreadCount": n}` for the app's badge |
| `POST /api/v1/notifications/{id}/read` | Mark one notification read (reading again keeps the first read time) |
| `POST /api/v1/notifications/read-all` | Mark every live notification read |

### Admin portal routes (no `X-Site-Id`; actor from `X-User`)

| Route | Purpose |
|---|---|
| `POST /api/v1/portal/notifications` | Publish an announcement: `category`, `title`, `description`, `linkUrl?`, `targetSiteCodes`, `publishAt?` (start), `expiresAt?` (end) |
| `POST /api/v1/portal/notifications/{id}/withdraw` | Withdraw a published announcement (system notices cannot be withdrawn) |
| `GET /api/v1/portal/notifications/{id}/delivery` | Targets, delivered, read, and per store when it was delivered and read |

- Business routes live under `/api/v1`.
- The API starts even if the database is down; `/health/ready` reports 503 until it is back.
- Every element and device carries `status` (colour: `green`, `yellow`, `red`) and
  `connectivity` (`online`, `degraded`, `unknown`, `offline`).
- Incident `id` is the incident number (`INC00001234`). `ciType`, `appointmentType` and the
  notification `category` replace the mock's icons; the frontend maps them to icons.
- Incident display rules (`app/api/mappers/incidents.py`): severity from priority (1-2 high,
  3 medium, 4-5 low); badge from the hold reason when on hold (`Pending Site Visit`, ...), otherwise
  `Open`; summary is the latest comment written by IT staff, otherwise the description;
  `resolutionETA` is the ETA in store time (`Today 2PM IST`), otherwise the ETA note.

### Request headers

| Header | Required | Purpose |
|---|---|---|
| `X-Site-Id` | Mobile routes | The store, e.g. `01234`. Missing or malformed: 422. Unknown store: 404. |
| `X-User` | No | The person acting (audit trail, replies, reads). Default: `store-<site>` on mobile, `portal-user` on the portal. |
| `X-Client-App` | No | `mobile_app` or `admin_portal`, recorded in the request log (otherwise `other`). |
| `X-Request-ID` | No | A UUID chosen by the client; otherwise the API generates one. Always returned in the response. |

`X-User` and the store header are placeholders until Entra ID: then both come from the signed-in
user's token, and only `app/api/context.py` changes.

### Responses

- JSON field names are camelCase (the names the mobile app already uses).
- Dates are ISO 8601 in UTC with a `Z` suffix, e.g. `2026-10-13T07:12:00Z`.
- Display text such as `Today at 1:00 PM` or `Today 2PM IST` is formatted in the store's timezone.
- Every error has the same shape:
  `{"error": {"code": "...", "message": "...", "requestId": "...", "details": []}}`.

## Code layers

| Layer | Folder | Job |
|---|---|---|
| Route | `app/api/routes/` | HTTP only: read the request, call a query or service, return the schema |
| Dependencies | `app/api/deps.py`, `context.py` | Per request: session, clock, store (`X-Site-Id`), actor |
| Query | `app/queries/` | Read the data one screen needs, filtered by store; never writes |
| Mapper | `app/api/mappers/` | Database values to display values (pure functions) |
| Schema | `app/api/schemas/` | The JSON contract (camelCase, UTC dates) |
| Service | `app/services/` | One complete business operation: rules, locks, rows, audit, notifications, one commit |

The worker, the seed and later the MCP servers call the same queries and services as the routes.

## ITSM: incidents, journal and appointments

- `app/services/incidents.py` is the only code that writes incidents and journal entries:
  `create_incident`, `add_journal_entry`, `update_incident`, `resolve_incident`, `close_incident`,
  `cancel_incident`. `app/services/appointments.py` is the only code that writes appointments.
- Every write locks the incident row, checks the rules, writes the change with its journal entries
  and audit rows, and commits once (or only flushes with `commit=False`, used by seeds and tests).
- State changes: New, In Progress and On Hold move between each other and to Resolved or Canceled;
  Resolved can only be Closed; Closed and Canceled are final (no reopening in the POC).
- The journal is also the worker's queue. Pending: the system "Incident created." entry and every
  work note or comment by IT staff (resolving writes the close notes as a pending work note).
  Skipped: store users' replies and entries by the agent or the system.
- Resolving closes the store alert; open actions stay open. Closing or cancelling also closes
  the actions and cancels the incident's scheduled appointments (each with a store notification).
- Every appointment is tagged to an active incident of the same store (`incident_id` is NOT NULL
  since revision `0002`); appointments come from the admin portal.
- New incident numbers come from the `incident_number_seq` sequence (from `INC00002000`).

## Notifications

- `app/services/notifications.py` is the only code that writes `notifications` and
  `notification_targets`: one row per message, one target row per store (which also holds that
  store's `delivered_at`, `read_at` and `read_by`).
- Portal announcements go to an explicit list of site codes (the portal resolves "all stores" or a
  region). An unknown site code rejects the whole request. A start time in the past means now; in
  the future it schedules the announcement. Only published announcements can be withdrawn.
- System notifications use fixed templates (`app/services/notification_templates.py`), never the
  LLM, and always have exactly one target. Appointment notices ("New Appointment: ... at 01:00 PM",
  rescheduled, cancelled) are written by the appointment service in the same transaction; status
  updates (in progress, completed, no-show) send nothing. Incident update and action notices are
  created by the worker (Phase 9). Stored text uses absolute store-local times
  (`Tue 13 Oct at 1:00 PM IST`), never "Today".
- A notification is live for a store when it has a target row, is published, `publish_at <= now`
  and it has not expired. Scheduling, expiry and withdrawal need no background job.
- Expiry: appointment notices 24 hours after the visit ends; incident and action notices after
  7 days; announcements when the portal's end time says so.
- Delivery is pull-based: the app fetches the list and polls the unread count. Push notifications
  can be added later as an extra step after the rows are written.

## LLM layer

`app/llm/` sends a prompt to a model and returns JSON that matches the prompt's schema. The worker
(Phase 9) depends only on the `LlmProvider` interface; settings decide which provider answers.

| Provider | `LLM_PROVIDER` | Default model | Needs |
|---|---|---|---|
| NVIDIA NIM | `nvidia` | `nvidia/nemotron-3.5-lightning-30b-a3b` | `LLM_API_KEY` (nvapi-...) |
| Groq | `groq` | `openai/gpt-oss-20b` | `LLM_API_KEY` (gsk_...) |
| Azure AI Foundry / Azure OpenAI | `foundry` | your deployment | `LLM_API_KEY`, `LLM_MODEL`, `LLM_BASE_URL` |
| Fake (no network) | `fake` | - | nothing |

- Switching provider is a `.env` change; check it with `uv run python -m scripts.llm_check`
  (`--fallback` checks only the fallback provider).
- All three real providers use one OpenAI-compatible class (`app/llm/providers.py`); their
  differences (base URL, default model, how JSON is enforced, token limit name) are presets.
- JSON is enforced by the provider where possible (`LLM_STRUCTURED_OUTPUT=auto`: NVIDIA guided
  JSON, Groq and Foundry JSON mode) and always validated against the schema
  (`app/llm/validation.py`): code fences and `<think>` blocks are stripped; anything that does not
  match is `LlmInvalidOutput`.
- Errors: `LlmUnavailable` (timeout, network, 429, 5xx: retry later), `LlmRejected` (bad key,
  unknown model, 4xx: fix the settings), `LlmInvalidOutput` (bad answer). No hidden retries: the
  worker retries. Free endpoints can be slow, so `LLM_TIMEOUT_SECONDS` defaults to 120.
- Optional fallback provider (`LLM_FALLBACK_*`), used only when the primary is unavailable.
- Prompts come from the active `ai_prompt_versions` row (`app/llm/prompts.py`); `{context_json}`
  is filled with the call's context. Every call is recorded in `ai_workflow_runs`
  (`app/llm/runs.py`): the running row is committed before the call, the model is called outside
  any transaction, and `model_name` stores `provider:model` of the model that answered.
- Tests never call the network; `tests/test_llm_live.py` makes one real call only when
  `LLM_LIVE_TEST=true`.
- On NVIDIA's free tier, inputs and outputs may be recorded by NVIDIA: use mock data only.

## Worker: alerts, actions and incident notifications

The worker (`app/worker/`, run with `scripts.worker`) turns pending incident journal entries into
store-facing messages with the two LLM workflows. It is a separate process from the API.

| Pending entry | Workflow | Result |
|---|---|---|
| `system` "Incident created." (active incident) | Impact assessment | One store alert per incident (no notification) |
| IT work note or comment | Update classification | action (+ "Action Required" notification), notify ("Incident ... Updated" notification) or none |
| Close notes of a resolved incident | Update classification (resolution) | Usually a yes/no "confirm the fix" action |

- Skipped without an LLM call: "created" entries of incidents no longer active, notes written before
  the incident was resolved, anything on closed or cancelled incidents, entries not written by IT.
- A comment is IT writing to the store: it is always sent (as written). A work note is internal:
  the store sees the model's store-friendly rewrite, or nothing.
- Alerts, actions and notifications take the journal entry's time, so seeded data keeps its timeline.
- Every call is recorded in `ai_workflow_runs`. Errors: unavailable or invalid answers are retried
  (`WORKER_RETRY_DELAYS_MINUTES`); a rejected request (bad key or model) is not retried. After the
  last attempt an impact assessment writes a template alert (run `needs_review`) so a store never
  misses an outage; a classification is marked `failed`.
- Entries of one incident are handled in order; claiming uses `FOR UPDATE SKIP LOCKED`.
- Open actions past their due time are closed as `expired` (every `WORKER_SWEEP_SECONDS`), and
  `/actions` never shows them.
- A store's reply to an action is only recorded: a store comment on the incident (`skipped`, never
  sent to the LLM) and a `store_action_responses` row; the action becomes `responded` until IT
  closes it or the incident is closed (`app/services/action_responses.py`).
- Free-tier quotas: at most `WORKER_MAX_CALLS_PER_MINUTE` calls (default 20), a pause after HTTP 429,
  and `--max-calls` to cap a drain. A full drain of the seed data makes about 26 calls.

```powershell
uv run python -m scripts.seed all                  # reset the demo data (all entries pending)
uv run python -m scripts.worker --drain            # process everything pending, then stop
uv run python -m scripts.worker --drain --max-calls 5
uv run python -m scripts.worker                    # keep running (Ctrl+C to stop)
```

## LLM rules, scorecard and verification

The worker's prompts (version 2, `seed/data/prompts.yaml`) guide the model; the guards in
`app/worker/guards.py` enforce the rules in code on every answer. When a guard changes an answer,
`ai_workflow_runs.output` holds `{"answer", "applied", "adjustments"}`.

- Update classification, three gates in order:
  1. **Request -> action** only when IT's update explicitly asks the store to do or report
     something. The model must quote those words (`store_request_quote`); code checks the quote is
     in the update and reads as a request to the store, otherwise the action becomes a notification.
  2. **Information -> notify**: progress, a visit, a workaround, a changed fix time.
  3. **Nothing -> none**: internal technical detail. A comment is never `none` (sent as written).
- **At most one open action per incident** (still waiting for the store). Answered actions do not
  count. Enforced by the worker, `app/services/actions.py` and the database index
  `ux_action_one_open_per_incident` (revision `0003`). A second request becomes a notification and
  the AI run is marked `needs_review` for IT.
- A resolution always asks a yes/no "is it fixed" question, replacing an older open action.
- A due time is kept only when the update states a clock time (`due_time_quote`).
- Alerts: severity within the range allowed by the incident priority and the main CI's criticality;
  no clock times. No store-facing text may contain IP/MAC addresses, device codes or serial
  numbers, ticket or circuit ids, staff user names or e-mail addresses.
- After the last failed attempt: impact -> template alert; confirm_fix -> template yes/no question;
  comment -> sent as written; work note -> entry `failed`.

```powershell
uv run python -m scripts.llm_eval --list          # the 18 scorecard cases (no LLM call)
uv run python -m scripts.llm_eval                 # scorecard against the configured model (~18 calls)
uv run python -m scripts.llm_eval --case trying-to-reach-store --repeat 3
uv run python -m scripts.verify                   # check the database follows the rules (no LLM call)
uv run python -m scripts.demo                     # seed all -> worker drain -> verify (~25 calls)
uv run python -m scripts.demo --reset --yes       # the same from an empty schema
```

LLM wording varies between runs; everything else is deterministic, and `scripts.verify` proves the
rules hold. Without an NVIDIA key, `LLM_PROVIDER=fake` runs the whole flow with placeholder text.

## Network event processing

Raw Aruba events (`raw_network_events`) are turned into `network_events` (typed history) and
`network_element_current_status` (one row per element: the status the `/network` screen shows).

```powershell
uv run python -m scripts.process_events                 # process pending events
uv run python -m scripts.process_events --rebuild       # clear and replay everything
uv run python -m scripts.process_events --rebuild --as-of 2026-10-13T09:00:00Z
```

- Events are processed in arrival order (`ingestion_time`). Only events that arrived by `--as-of`
  (default: now) are processed.
- An event changes an element's status only if it is newer (by event time) than the newest
  status already applied; late, older events are stored with `status_applied = false`.
- Duplicates (same Aruba event id), unknown sites and unknown elements are recorded on the raw row
  (`duplicate`, `unmatched`); malformed payloads are marked `error` and do not stop the run.
- Every run is recorded in `event_processing_runs`. Two runs never overlap (advisory lock).
- The Aruba payload format lives only in `app/processing/aruba.py`.

## Day-to-day

| Command | Purpose |
|---|---|
| `uv run uvicorn app.main:app --reload` | Run the API locally |
| `uv run ruff check .` | Lint |
| `uv run ruff format .` | Format |
| `uv run pytest` | Run tests |
| `uv add <pkg>` | Add a dependency (updates uv.lock; commit both files) |
| `uv run alembic upgrade head` | Apply new migrations after pulling changes |
| `uv run python -m scripts.db_status` | Show revision and table count |
| `uv run python -m scripts.db_reset --yes` | Drop and recreate all tables (local/test only, data is lost) |
| `uv run python -m scripts.seed all` | Load or refresh all seed data |
| `uv run python -m scripts.seed all --reset --yes` | Rebuild the schema, then load all seed data |
| `uv run python -m scripts.seed itsm` | Reset incidents, journal entries, appointments and appointment notices |
| `uv run python -m scripts.seed notifications` | Reset the seeded portal announcements |
| `uv run python -m scripts.process_events --rebuild` | Rebuild the network status from the raw events |
| `uv run python -m scripts.llm_check` | One test call to the configured LLM provider (`--fallback` for the fallback) |
| `uv run python -m scripts.worker --drain` | Process all pending journal entries with the LLM, then stop (uses LLM quota) |
| `uv run python -m scripts.demo` | Seed all, drain the worker with the configured LLM, verify (`--reset --yes` from an empty schema) |
| `uv run python -m scripts.verify` | Check the database follows the agreed rules (no LLM call) |
| `uv run python -m scripts.llm_eval` | Scorecard of the LLM workflows (18 calls; `--list`, `--case`, `--repeat`) |
| `uv run python -m scripts.request_log_tail` | Show the last API requests (`-n 50`, `--site 01234`) |

## Seed data

- Data files are in `seed/data/` (YAML); the code is in `app/seed/`.
- `uv run python -m scripts.seed <step>` with step `master`, `inventory`, `events`, `network`,
  `itsm`, `notifications` or `all`.
  - `master`: 10 stores, assignment groups, network element and device types, prompt versions.
  - `inventory`: 13 network elements and 17 devices per store, from `inventory_template.yaml`
    plus `store_overrides.yaml`.
  - `events`: replaces the 24-hour window of raw Aruba events (all `pending`).
  - `network`: runs the event processor (`--rebuild`, as of the anchor).
  - `itsm`: replaces incidents (`incidents.yaml`), their journal entries (all `pending`, for the
    worker), appointments (`appointments.yaml`) and one "New Appointment" notice per appointment
    (published at its `booked_at`), and clears anything derived from them (alerts, actions,
    AI runs, incident and appointment notifications).
  - `notifications`: replaces the portal announcements (`notifications.yaml`), published through
    the notification service: all stores, a region or single stores; already read, scheduled,
    expired and withdrawn examples.
- Times are offsets from an anchor (the run time, or `--anchor 2026-10-13T09:00:00Z`),
  so the demo data always looks current. Appointment times and firm ETAs are store-local clock
  times (e.g. 12:00 IST): today, or tomorrow if that time has already passed when the seed runs.
- IDs are deterministic (`uuid5`), so every machine gets the same ids for the same data
  (seeded portal announcements get new ids on every run).
- Store `01234` (Chennai Anna Nagar) carries the scenarios of the mobile app mock data: incidents
  INC00001234-INC00001237, appointments apt-001 to apt-003, and notifications n-001, n-002, n-004,
  n-005 (2 PM), n-008 to n-011. Seed it in the Chennai morning to see the visits as
  "Today at 12:00 PM / 1:00 PM / 2:00 PM", exactly as in the mock.
- Safe to run more than once. To change data, edit the YAML files and run the seed again.

## Database schema

- The schema is defined in plain SQL in `migrations/sql/` and applied by Alembic revisions in
  `migrations/versions/`. `0001` creates the full POC schema (22 tables); `0002` adds the incident
  number sequence and makes `appointments.incident_id` required; `0003` allows at most one open store action per incident. 0004 adds the GLPI sync: GLPI ids on stores, assignment groups, network elements,
  devices, incidents and journal entries, plus the itsm_inbox and itsm_sync_state tables (24 tables).
- Models in `app/models/` describe columns and keys only. CHECK constraints, partial indexes and
  triggers live in the database. `tests/test_models_match_db.py` fails if models and database differ.
- Allowed values for constrained text columns are in `app/models/values.py`.
  `tests/test_values_match_db.py` fails if they differ from the database.
- New schema changes: add a new revision with
  `uv run alembic revision -m "<what changed>" --rev-id 0003` and write the `op.*` calls by hand.
  Do not rely on `--autogenerate`: it does not understand the SQL-defined checks, indexes and triggers.

## GLPI sync schema (revision 0004)

GLPI is the ITSM of record; RIC keeps a synchronized copy of the incidents of its stores.
Phase 13 adds only the schema; the sync itself comes in later phases.

| Table / column | Purpose |
|---|---|
| `stores.glpi_location_id` | GLPI Location of the store (location name starts with the site code) |
| `assignment_groups.glpi_group_id` | GLPI Group |
| `network_elements` / `devices`: `glpi_itemtype`, `glpi_item_id` | The GLPI asset (NetworkEquipment, Computer, Printer, Peripheral); both or neither; unique as a pair |
| `incidents.glpi_ticket_id` | GLPI Ticket |
| `incidents.itsm_updated_at` | GLPI `date_mod` of the last applied snapshot; older snapshots are ignored |
| `incidents.last_synced_at` | When RIC last applied a GLPI snapshot |
| `incident_journal.glpi_followup_id` | GLPI follow-up; one journal row per follow-up |
| `itsm_inbox` | Sync work queue: one row per GLPI ticket to sync; at most one pending row per ticket, so webhooks arriving together are merged |
| `itsm_sync_state` | Scheduled sync jobs: next run, watermark, last result |

All GLPI ids are unique: one GLPI object can never be linked to two RIC rows.
New allowed values: `incident_journal.source` = `glpi`, `audit_log.source` = `itsm_sync`.
The `servicenow_sys_id` columns are kept and are not used by the GLPI sync.

## Code layout

| Path | Contents |
|---|---|
| `app/main.py` | `create_app()` factory and lifespan (engine created at startup, disposed at shutdown) |
| `app/core/` | Settings, logging, request id middleware, request log, errors, clock, time formatting, actor, status colours |
| `app/api/` | Dependencies (`deps.py`, `context.py`), the `/api/v1` router, mappers, schemas and route modules |
| `app/queries/` | Read-side SQL for the API, one module per screen, always filtered by store |
| `app/services/` | Business services, the only code that writes business tables: incidents, appointments, notifications, audit |
| `app/llm/` | LLM layer: providers (NVIDIA NIM, Groq, Azure AI Foundry, fake), fallback, validation, prompts, run records |
| `app/worker/` | The worker: queue, routing rules, LLM context, loop with pacing, retries and action expiry |
| `app/processing/` | Network event processor: Aruba extractors, matching, status rules, runs |
| `app/db/` | Engine, sessions, Alembic helper |
| `app/models/` | SQLAlchemy models and allowed value lists |
| `app/seed/` | Seed data loaders and generators |

## Conventions

- asyncpg is the only PostgreSQL driver. The app connects through SQLAlchemy using `postgresql+asyncpg://`.
- Configuration is read only through `app.core.config.Settings`. No direct `os.environ` access.
- Nothing calls `datetime.now()` directly: routes use `ClockDep`, services receive the time or a clock.
- Mobile routes take the store from `StoreDep` and filter every query by `store.id`.
- Routes never write SQL: read queries live in `app/queries/`, writes in `app/services/`.
  A route uses a handful of queries in total, never one query per row.
- Routes never create sessions; they use the `SessionDep` dependency. Services that write call
  `commit()` themselves; read-only routes never commit.
- Services receive an `ActorContext`, write an audit row with `record_audit()` in the same
  transaction as the change, and raise `app.core.errors` exceptions (never HTTP errors).
- Response models inherit `ApiModel` (camelCase JSON) and use `UtcDateTime` for dates.
- Every script lives in `scripts/`, runs as `uv run python -m scripts.<name>` from the repo root,
  and is safe to run more than once.
- `.env` is never committed. `.env.example` is the template.

## GLPI client (Phase 14)

`app/itsm/` talks to GLPI 11 and turns its tickets into RIC values. It does not write
to the database; the sync (later phases) uses it.

| Module | Job |
|---|---|
| `glpi_client.py` | API V2 (OAuth2 password grant): ticket + team, timeline, changed-since search, add follow-up, write external id. API V1 (session) only where V2 has no equivalent: ETA (`time_to_resolve`), linked CI, pending reason, category parent, search by RIC number |
| `snapshot.py` | One ticket as read from GLPI (GLPI terms, UTC times) |
| `mapping.py` | Snapshot -> RIC values: state, hold reason, levels, category, contact type, "RIC details" fields, journal entries |
| `timeparse.py` | GLPI date/time text <-> UTC (`GLPI_TIMEZONE`) |
| `errors.py` | `ItsmUnavailable` (retry later), `ItsmRejected` (fix the cause), `ItsmNotFound` |
| `factory.py` | `build_itsm_client(settings)`; lists missing settings |

Settings: the ITSM section of `.env.example` (same names as `glpi-poc/.env.api`).
GLPI must be running (Docker Desktop).

```powershell
uv run python -m scripts.check_env                    # includes the GLPI login check
uv run python -m scripts.itsm_check                   # INC00001234 as RIC values (read-only)
uv run python -m scripts.itsm_check --number INC00001238
```

## Linking RIC to GLPI (Phase 15)

`scripts.itsm_link` finds, for every RIC row, the GLPI object that already exists for it and
stores its GLPI id. It creates nothing in GLPI and copies no incident content (the sync does).

| RIC row | GLPI object | Match |
|---|---|---|
| store | Location | name starts with `<site_code> - ` |
| assignment group | Group | same name |
| network element / device | NetworkEquipment, Computer, Printer, Peripheral | same name at the store's location |
| incident | Ticket | `external_id` = RIC number |
| journal entry | follow-up of that ticket | same kind (work note = private, comment = public) and text |

Then it queues one `itsm_inbox` row (source `link`) per incident ticket at a RIC store (its
first sync) and sets the `itsm_reconcile` watermark to the newest GLPI `date_mod`.
A match must be unique on both sides; anything else is reported and left unchanged; existing
links change only with `--relink`. Safe to run more than once.

```powershell
uv run python -m scripts.itsm_link --dry-run   # report only
uv run python -m scripts.itsm_link             # link, queue first syncs, set watermark
uv run python -m scripts.itsm_link --relink    # also move links that point elsewhere
```

Reseeding: `scripts.seed master` / `inventory` keep the links. `scripts.seed itsm` recreates
incidents and journal entries without them: run `scripts.itsm_link` again afterwards.

## Syncing GLPI tickets (Phase 16)

`app/itsm/sync.py` makes one RIC incident match its GLPI ticket: it reads a fresh snapshot
of the whole ticket (never acts on the event), then in one transaction (advisory lock per
ticket) applies it through `app/services/incidents.py`, then writes the RIC number back to
GLPI if it differs.

| GLPI | RIC |
|---|---|
| new incident ticket at a RIC store | new incident (its INC external id, or the next number) |
| fields, assignment, CI, ETA, state 1-3 | updated (an older snapshot never overwrites newer data) |
| Solved / Closed / solution type "Cancelled" | resolve / resolve + close / cancel, with the usual side effects |
| reopened after RIC finished it | `conflict`: RIC never reopens |
| new follow-up | journal entry (IT staff -> pending -> LLM worker); RIC's own writes and store replies skipped |
| edited follow-up | text updated, not processed again |
| not an incident, deleted, not a RIC store | `ignored` |

`itsm_inbox` is the work queue (one pending row per ticket). Rows queued by `scripts.itsm_link`
sync quietly: imported history is skipped, never sent to the LLM.

```powershell
uv run python -m scripts.itsm_sync --pending              # process the inbox queue
uv run python -m scripts.itsm_sync --number INC00001234   # one incident
uv run python -m scripts.itsm_sync --ticket 3             # one GLPI ticket
```

## GLPI webhooks (Phase 17)

GLPI calls `POST /api/v1/integrations/glpi/webhooks/{itemtype}/{event}` when a ticket is
created or updated, or a follow-up or solution is added. The route answers 202 in
milliseconds: it only records the ticket in `itsm_inbox` (one pending row per ticket;
deliveries that arrive together are merged, each keeping its full GLPI payload). The sync
(`scripts.itsm_sync --pending`, the worker from Phase 18) reads a fresh snapshot from GLPI.

| Check | Result |
|---|---|
| `X-GLPI-Signature` = HMAC-SHA256(`GLPI_WEBHOOK_SECRET`, body + `X-GLPI-Timestamp`) | else 401 |
| timestamp at most 24 h old, at most 5 min ahead (GLPI stamps when it queues) | else 401 |
| body at most 1 MB | else 413 |
| secret not configured | 503 |
| unknown type, Change/Problem follow-up, no ticket id, not JSON | 202 `accepted: false` (logged) |

No `X-Site-Id`: GLPI is not a store. The API must listen on all interfaces for GLPI (Docker):

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
uv run python -m scripts.itsm_webhooks            # create / update the 4 GLPI webhooks
uv run python -m scripts.itsm_webhooks --status
uv run python -m scripts.itsm_webhooks --disable
```

`ITSM_WEBHOOK_BASE_URL` (default `http://host.docker.internal:8000`) is the API as GLPI reaches it.

## GLPI sync in the worker (Phase 18)

With GLPI configured, `scripts.worker` runs three loops in one process:

| Loop | Does |
|---|---|
| journal (unchanged) | pending journal entries -> LLM -> alerts, actions, notifications |
| ITSM inbox | due `itsm_inbox` rows -> `sync_ticket` (fresh GLPI snapshot). GLPI unavailable: retry after 30 s, 2 min, 10 min, 30 min..., the loop pauses; GLPI rejected: failed; at most 8 attempts |
| scheduler | `itsm_reconcile` every 15 min: GLPI tickets changed after (watermark - 5 min) -> inbox; watermark = newest GLPI `date_mod`, moved only on success. Hourly: deletes done / ignored inbox rows older than 14 days |

The loops pass work through tables only, so one never blocks the others. The reconciliation
catches what webhooks cannot: reassignments, deliveries GLPI gave up on, changes while the
API was down. It needs the watermark set by `scripts.itsm_link`.

```powershell
uv run python -m scripts.worker                 # all loops (GLPI sync on when configured)
uv run python -m scripts.worker --drain         # inbox first, then the journal, then stop
uv run python -m scripts.worker --no-itsm       # journal only
uv run python -m scripts.itsm_sync --reconcile  # the 15-minute check, once, now
```

`scripts.demo` stays offline (it reseeds incidents without GLPI links). With GLPI:
`scripts.seed itsm` -> `scripts.itsm_link` -> `scripts.worker`.

## Store replies posted to GLPI (Phase 19)

`POST /api/v1/actions/{actionId}/responses` for an incident linked to GLPI posts the reply to the
GLPI ticket FIRST, as a public follow-up by the integration user:

    [Store 01234 - priya.store-manager] Store reply to "Is Printer 02 printing now?": Yes.

Only after GLPI accepted it is the reply recorded in RIC (as before), with the follow-up's
`glpi_followup_id` on the store comment, so GLPI's webhook for it changes nothing.

| Case | Answer |
|---|---|
| same `clientRequestId` again | 200, the earlier reply; nothing posted again |
| invalid / answered / closed / expired (checked before GLPI) | 422 / 409, nothing posted |
| GLPI unavailable | 503 `itsm_unavailable`, nothing recorded (retry with the same id) |
| GLPI refused | 502 `itsm_rejected`, nothing recorded |
| ticket deleted in GLPI | 409 |
| incident not linked to GLPI | recorded in RIC only, as before |

The API creates one GLPI client at startup; incomplete GLPI settings never stop the API.

## GLPI integration: operations (Phase 20)

### What runs

| Process | Command | Does |
|---|---|---|
| PostgreSQL | (service) | RIC database |
| GLPI + MySQL | `docker compose up -d` (GLPI folder) | the ITSM; its own cron sends the webhooks every minute |
| RIC API | `uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` | mobile + portal routes, GLPI webhooks in, store replies out to GLPI |
| RIC worker | `uv run python -m scripts.worker` | three loops: journal (LLM), ITSM inbox (sync GLPI tickets), scheduler (15-minute reconciliation, inbox clean-up) |

Start order: PostgreSQL and GLPI, then the API, then the worker. `--host 0.0.0.0` is needed
because GLPI calls the API from Docker (`ITSM_WEBHOOK_BASE_URL`, default
`http://host.docker.internal:8000`).

### Is the sync healthy?

```powershell
uv run python -m scripts.itsm_status          # from the database (exit code 1 on FAIL)
uv run python -m scripts.itsm_status --live   # plus GLPI logins and clock drift
```

The same report as JSON: `GET /api/v1/admin/itsm/status` (no `X-Site-Id`).

| Message | Meaning | Fix |
|---|---|---|
| reconcile FAIL "never set up" / "no watermark" | the 15-minute check has no starting point | `scripts.itsm_link`; with no ticket in GLPI yet: `scripts.itsm_resync --start-watermark` |
| reconcile WARN "last successful run ..." | the worker is not running | start `scripts.worker` |
| reconcile FAIL "N failed runs in a row" | GLPI unreachable or login refused | `scripts.itsm_status --live`, check Docker / `.env` |
| inbox WARN "oldest received ..." | rows wait: worker stopped or GLPI down | start the worker / GLPI |
| inbox WARN "'processing' for over 30 min" | a worker stopped in the middle | restart the worker (it releases them) |
| inbox WARN "failed row(s)" | a sync was refused (e.g. bad token) | fix the cause, then `scripts.itsm_resync --retry-failed` |
| inbox WARN "conflict(s)" | GLPI reopened / changed a finished incident (RIC never reopens) | review: `scripts.itsm_resync --list-conflicts` |
| links FAIL "no store is linked" | `itsm_link` never ran (or after a schema reset) | `scripts.itsm_link` |
| links INFO "... have no GLPI ticket" | incidents that exist only in RIC (e.g. reseeded) | `scripts.itsm_link` if they exist in GLPI |
| clock WARN / FAIL | Docker VM clock drifted (after sleep) | restart Docker Desktop (or `wsl --shutdown`) |
| webhooks INFO "no webhook received yet" | GLPI webhooks not set up / not reaching the API | `scripts.itsm_webhooks --status`; GLPI log `/var/glpi/logs/webhook.log` ("is unsafe" = URL allowlist not loaded) |

### Recovery (only queues work; the worker syncs)

```powershell
uv run python -m scripts.itsm_resync --number INC00001234   # one incident
uv run python -m scripts.itsm_resync --ticket 3             # one GLPI ticket
uv run python -m scripts.itsm_resync --all --quiet          # everything linked; history not sent to the LLM
uv run python -m scripts.itsm_resync --retry-failed         # failed rows again
uv run python -m scripts.itsm_resync --list-conflicts
uv run python -m scripts.itsm_resync --start-watermark      # reconciliation starts at GLPI's current time
uv run python -m scripts.itsm_sync --reconcile              # the 15-minute check now
```

| Situation | Steps |
|---|---|
| reseeded incidents (`scripts.seed itsm`) | `scripts.itsm_link`, then the worker syncs the queued rows quietly |
| GLPI restored from a backup | `scripts.itsm_link`, `scripts.itsm_resync --all --quiet` |
| new webhook secret | change `GLPI_WEBHOOK_SECRET` in `.env`, restart the API, `scripts.itsm_webhooks` |
| GLPI was down for hours | nothing: retries and the 15-minute check catch up; `scripts.itsm_status` to confirm |

### End-to-end check before a demo (API + worker running)

| # | Do | Expect |
|---|---|---|
| 1 | GLPI: new ticket at location `01234 - ...`, category Network > WAN, group Network Ops | within ~1 min a new INC incident at store 01234 (number written to the GLPI external id); an alert for the store |
| 2 | GLPI: private follow-up "Store please restart the router and confirm." | store action + "Action Required" notification |
| 3 | App / Swagger: reply to that action | public follow-up `[Store 01234 - ...]` in the GLPI timeline; no new journal entry after its webhook |
| 4 | GLPI: change only the assigned group (no webhook) | `scripts.itsm_sync --reconcile` brings it over |
| 5 | Stop the API, add a follow-up in GLPI, start the API | the 15-minute check (or `--reconcile`) brings it over |
| 6 | GLPI: add a solution (Solved (Permanently)) | incident resolved, alert closed, "is it fixed?" action |
| 7 | GLPI: close the ticket | incident closed, actions closed, visits cancelled |
| 8 | GLPI: reopen it | `scripts.itsm_status` shows a conflict |

## Running RIC with GLPI on a new machine

For a colleague who has **PostgreSQL** and **Docker Desktop** but no GLPI yet. Everything below
runs RIC end to end: GLPI in Docker, the RIC API, the worker. Commands are for Windows
PowerShell (on macOS / Linux use the same commands in a terminal; paths differ).

You will create a few files **outside this repository** (in `C:\ric-glpi`); their full content
is in this section. Nothing in the repository needs to change.

### 1. Prerequisites

- Git, and `uv` (`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`,
  or `pip install uv` where install scripts are blocked)
- PostgreSQL 13+ running, with the `postgres` superuser password at hand
- Docker Desktop running (`docker version` answers)
- Ports free: 8000 (RIC API), 8080 (GLPI), 5432 (PostgreSQL)

### 2. RIC backend without GLPI

```powershell
git clone <repo-url> ric-backend; cd ric-backend
uv python install 3.12
uv sync
Copy-Item .env.example .env
```

Edit `.env`: `DATABASE_URL` (password of the `ric_app` user the next command creates),
`ADMIN_DATABASE_URL` (your `postgres` password), and the LLM: `LLM_PROVIDER=fake` works with
no key (placeholder texts); `LLM_PROVIDER=nvidia` + `LLM_API_KEY=nvapi-...` for real texts.
Leave the ITSM section for step 5.

```powershell
uv run python -m scripts.db_bootstrap       # role ric_app, databases ric_poc + ric_poc_test
uv run alembic upgrade head                 # all tables
uv run python -m scripts.db_status          # status OK
uv run python -m scripts.seed all           # 10 stores, inventory, demo incidents
uv run pytest -q                            # optional: every test passes without GLPI
uv run python -m scripts.check_env          # all OK except the GLPI line (until step 5)
```

### 3. GLPI in Docker

Create the folder `C:\ric-glpi` and these three files in it.

`C:\ric-glpi\docker-compose.yml`:

```yaml
name: ric-glpi
services:
  glpi:
    image: glpi/glpi:11.0.11
    restart: unless-stopped
    env_file: .env
    depends_on:
      - db
    ports:
      - "8080:80"
    extra_hosts:
      - "host.docker.internal:host-gateway"
    volumes:
      - glpi_data:/var/glpi
      - ./local_define.php:/var/www/glpi/config/local_define.php:ro
  db:
    image: mysql:8.4
    restart: unless-stopped
    environment:
      MYSQL_RANDOM_ROOT_PASSWORD: "yes"
      MYSQL_DATABASE: ${GLPI_DB_NAME}
      MYSQL_USER: ${GLPI_DB_USER}
      MYSQL_PASSWORD: ${GLPI_DB_PASSWORD}
    volumes:
      - db_data:/var/lib/mysql
volumes:
  glpi_data:
  db_data:
```

`C:\ric-glpi\.env` (choose your own password; keep this file private):

```text
GLPI_DB_HOST=db
GLPI_DB_PORT=3306
GLPI_DB_NAME=glpi
GLPI_DB_USER=glpi
GLPI_DB_PASSWORD=change-me-glpi-db
```

`C:\ric-glpi\local_define.php` (lets GLPI send webhooks to the RIC API on this machine; GLPI
11 refuses webhook URLs with a port unless they are allowed here):

```php
<?php
define('GLPI_SERVERSIDE_URL_ALLOWLIST', [
    '/^(https?|feed):\/\/[^@:]+(\/.*)?$/',
    '/^https?:\/\/host\.docker\.internal(:\d+)?(\/.*)?$/',
]);
```

Start it (the first start installs GLPI; wait until the log is quiet, 1-3 minutes):

```powershell
cd C:\ric-glpi
docker compose up -d
docker compose logs -f glpi       # Ctrl+C when the installation has finished
docker compose ps                 # glpi and db: running
```

Open http://localhost:8080 and log in as `glpi` / `glpi`. Change that password
(top right > My settings). Delete or deactivate the other default users (`tech`, `normal`,
`post-only`) under Administration > Users if you like.

### 4. Configure GLPI (in the browser, as glpi)

1. **API** - Setup > General > tab **API**: set *Enable API*, *Enable legacy API* (REST API),
   *Enable login with credentials* and *Enable login with external token* to **Yes**. Save.
2. **API client** - same tab, *Add API client*: name `ric-backend`, Active **Yes**, leave the
   IPv4 / IPv6 ranges **empty** (RIC's calls arrive through Docker, not from 127.0.0.1),
   *Regenerate* the application token, Save. Copy the **Application token** (`GLPI_APP_TOKEN`).
3. **Integration user** - Administration > Users > Add: login `ric.api`, a password
   (`GLPI_API_PASSWORD`), profile **Super-Admin**, entity **Root entity**, recursive **Yes**.
   Save, open `ric.api` again, section *Remote access keys*: tick *Regenerate* next to
   **API token**, Save, copy the token (`GLPI_USER_TOKEN`).
4. **OAuth client** (API V2) - Setup > OAuth clients > Add: name `ric-backend`, Grants
   **Password**, Scopes **api**. Save, copy **Client ID** and **Client secret**.
5. **Webhook sender** - Setup > Automatic actions > `queuedwebhook`: Status *Scheduled*,
   run mode *CLI*, frequency *1 minute* (the defaults of the Docker image; change if not).

### 5. Connect RIC to GLPI

Generate a webhook secret:

```powershell
uv run python -c "import secrets; print(secrets.token_hex(32))"
```

In `ric-backend\.env`, fill the ITSM section (names as in `.env.example`):

```text
ITSM_PROVIDER=glpi
GLPI_V2_URL=http://localhost:8080/api.php/v2
GLPI_OAUTH_TOKEN_URL=http://localhost:8080/api.php/token
GLPI_OAUTH_CLIENT_ID=<Client ID from 4.4>
GLPI_OAUTH_CLIENT_SECRET=<Client secret from 4.4>
GLPI_API_USERNAME=ric.api
GLPI_API_PASSWORD=<password from 4.3>
GLPI_V1_URL=http://localhost:8080/apirest.php
GLPI_APP_TOKEN=<Application token from 4.2>
GLPI_USER_TOKEN=<API token from 4.3>
GLPI_WEBHOOK_SECRET=<the generated secret>
```

Keep the other ITSM lines of `.env.example` as they are. Then:

```powershell
uv run python -m scripts.check_env    # now also: [OK] GLPI: API V2 token and API V1 session as ric.api
```

**GLPI reference data.** RIC matches GLPI locations, groups, categories, request sources,
pending reasons and solution types by name. Create `C:\ric-glpi\glpi_bootstrap.py` with this
content (it reads the stores and groups from your RIC database and creates what is missing in
GLPI; safe to run again):

```python
"""Create the GLPI reference data RIC expects (run from the ric-backend folder).

Locations "<site code> - <store name>" for every RIC store, a group for every RIC
assignment group, the ITIL categories, request sources, pending reasons and solution
types RIC maps. Uses the GLPI_* values of ric-backend/.env (API V1: app token + the
ric.api user token). Safe to run more than once: existing items are kept.
"""

import asyncio
import html

import httpx
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import create_engine_from_settings, create_session_factory
from app.models import AssignmentGroup, Store

CATEGORIES = {
    "Network": ["WAN", "Wireless", "LAN"],
    "Hardware": ["POS", "Printer", "Scanner"],
    "Software": ["Register", "Back Office"],
    "Inquiry": [],
}
REQUEST_TYPES = ["Monitoring", "Phone", "E-Mail", "Helpdesk", "Direct"]
PENDING_REASONS = [
    "Awaiting caller",
    "Awaiting vendor",
    "Awaiting site visit",
    "Awaiting change",
    "Awaiting problem",
]
SOLUTION_TYPES = ["Solved (Permanently)", "Workaround", "Cancelled"]
SHARED = {"entities_id": 0, "is_recursive": 1}


class Glpi:
    def __init__(self, url, app_token, user_token, transport=None):
        self.http = httpx.Client(base_url=url.rstrip("/") + "/", timeout=30, transport=transport)
        self.headers = {"Content-Type": "application/json"}
        if app_token:
            self.headers["App-Token"] = app_token
        auth = {**self.headers, "Authorization": f"user_token {user_token}"}
        response = self.http.get("initSession", headers=auth)
        if response.status_code != 200:
            raise SystemExit(f"[FAIL] GLPI login: HTTP {response.status_code} {response.text}")
        self.headers["Session-Token"] = response.json()["session_token"]
        self.created = self.kept = 0

    def find(self, itemtype, name, parent_field=None, parent_id=None):
        params = {"searchText[name]": name, "range": "0-999"}
        response = self.http.get(itemtype, params=params, headers=self.headers)
        if response.status_code == 400 and "ERROR_RANGE_EXCEED_TOTAL" in response.text:
            return None
        response.raise_for_status()
        for item in response.json() if isinstance(response.json(), list) else []:
            same_name = html.unescape(str(item.get("name") or "")).strip() == name
            same_parent = parent_field is None or int(item.get(parent_field) or 0) == parent_id
            if same_name and same_parent:
                return int(item["id"])
        return None

    def ensure(self, itemtype, name, parent_field=None, parent_id=0, **fields):
        found = self.find(itemtype, name, parent_field, parent_id)
        if found is not None:
            self.kept += 1
            return found
        body = {"name": name, **fields}
        if parent_field:
            body[parent_field] = parent_id
        response = self.http.post(itemtype, json={"input": body}, headers=self.headers)
        if response.status_code not in (200, 201):
            raise SystemExit(
                f"[FAIL] add {itemtype} {name!r}: HTTP {response.status_code} {response.text}"
            )
        self.created += 1
        answer = response.json()
        answer = answer[0] if isinstance(answer, list) else answer
        print(f"  created {itemtype:<13} {name}")
        return int(answer["id"])

    def close(self):
        self.http.get("killSession", headers=self.headers)
        self.http.close()


async def ric_data():
    engine = create_engine_from_settings()
    try:
        async with create_session_factory(engine)() as session:
            stores = (
                await session.execute(
                    select(Store.site_code, Store.name, Store.city).order_by(Store.site_code)
                )
            ).all()
            groups = (
                await session.scalars(select(AssignmentGroup.name).order_by(AssignmentGroup.name))
            ).all()
    finally:
        await engine.dispose()
    return stores, groups


def main(transport=None):
    settings = get_settings()
    secret = lambda value: value.get_secret_value() if value else None  # noqa: E731
    if not settings.glpi_v1_url or not settings.glpi_user_token:
        raise SystemExit("[FAIL] set GLPI_V1_URL and GLPI_USER_TOKEN in ric-backend/.env first")
    stores, groups = asyncio.run(ric_data())
    if not stores:
        raise SystemExit("[FAIL] no store in the RIC database: run scripts.seed all first")
    glpi = Glpi(
        settings.glpi_v1_url,
        secret(settings.glpi_app_token),
        secret(settings.glpi_user_token),
        transport,
    )
    try:
        for site_code, name, city in stores:
            glpi.ensure(
                "Location", f"{site_code} - {name}", "locations_id", 0, town=city or "", **SHARED
            )
        for name in groups:
            glpi.ensure(
                "Group",
                name,
                is_requester=1,
                is_watcher=1,
                is_assign=1,
                is_notify=1,
                is_usergroup=1,
                **SHARED,
            )
        for parent, children in CATEGORIES.items():
            parent_id = glpi.ensure(
                "ITILCategory",
                parent,
                "itilcategories_id",
                0,
                is_incident=1,
                is_request=1,
                **SHARED,
            )
            for child in children:
                glpi.ensure(
                    "ITILCategory",
                    child,
                    "itilcategories_id",
                    parent_id,
                    is_incident=1,
                    is_request=1,
                    **SHARED,
                )
        for name in REQUEST_TYPES:
            glpi.ensure("RequestType", name, is_active=1, is_ticketheader=1, is_itilfollowup=1)
        for name in PENDING_REASONS:
            glpi.ensure("PendingReason", name, **SHARED)
        for name in SOLUTION_TYPES:
            glpi.ensure("SolutionType", name, **SHARED)
    finally:
        glpi.close()
    print(f"[OK] GLPI reference data: {glpi.created} created, {glpi.kept} already there")


if __name__ == "__main__":
    main()
```

Run it from the `ric-backend` folder (so it uses RIC's settings and database):

```powershell
uv run python -c "import runpy; runpy.run_path(r'C:\ric-glpi\glpi_bootstrap.py', run_name='__main__')"
```

Expected: `created Location 01234 - Chennai Anna Nagar` ... and `[OK] GLPI reference data:
29 created` (`0 created` on a second run). Then link RIC to GLPI and give the 15-minute check
its starting point:

```powershell
uv run python -m scripts.itsm_link              # stores 10 linked, groups linked; incidents 0 linked (none in GLPI yet)
uv run python -m scripts.itsm_resync --start-watermark
uv run python -m scripts.itsm_status --live     # verdict OK or WARN (worker not started yet)
```

The demo incidents from `scripts.seed` exist only in RIC (this GLPI has no copy of them); the
app shows and handles them as before, and replies to their actions stay in RIC. Tickets you
create in GLPI from now on are synced in both directions.

### 6. Start RIC

Windows only, once, in an **administrator** PowerShell (GLPI in Docker must reach port 8000):

```powershell
New-NetFirewallRule -DisplayName "RIC API 8000 from GLPI" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Any
```

Terminal 1 (ric-backend) - the API, reachable from Docker:

```powershell
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Terminal 2 (ric-backend) - check GLPI reaches the API, then create the GLPI webhooks:

```powershell
docker compose -f C:\ric-glpi\docker-compose.yml exec glpi php -r "echo file_get_contents('http://host.docker.internal:8000/health'), PHP_EOL;"
uv run python -m scripts.itsm_webhooks          # 4 webhooks "RIC - ...", status OK
uv run python -m scripts.worker                 # first line ends with "GLPI sync on"
```

Leave terminals 1 and 2 running.

### 7. Try it

1. GLPI > Assistance > Create ticket: title `Wi-Fi down on sales floor`, description, **Location**
   `01234 - Chennai Anna Nagar`, **Category** Network > Wireless, Urgency / Impact High,
   *Assigned to*: group **Network Ops**. Add.
2. Within about a minute: terminal 1 logs `POST /api/v1/integrations/glpi/webhooks/ticket/new 202`,
   terminal 2 logs `glpi ticket synced ... outcome=done`. Open http://127.0.0.1:8000/docs,
   `GET /api/v1/incidents` with header `X-Site-Id: 01234`: the new INC incident is listed
   (GLPI's ticket gets the same number as its external id). `GET /api/v1/alerts` shows its alert
   once the worker's LLM step ran.
3. In the GLPI ticket: Answer, **private** follow-up: `Store please restart the access point in
   the back office and confirm.` A minute later `GET /api/v1/actions` (X-Site-Id 01234) lists an
   action.
4. Reply in Swagger: `POST /api/v1/actions/{actionId}/responses`, headers `X-Site-Id: 01234`,
   `X-User: priya.store-manager`, body `{"clientRequestId": "<any new UUID>"}` (add
   `"answerYesNo": true` for a yes/no action, `"responseText": "..."` for a free-text one).
   The reply appears in the GLPI timeline as `[Store 01234 - priya.store-manager] ...`.
5. In GLPI add a **solution** (type Solved (Permanently)): the RIC incident becomes Resolved and
   the store gets an "is it fixed?" action.
6. `uv run python -m scripts.itsm_status`: verdict OK.

### 8. Every day

```powershell
docker compose -f C:\ric-glpi\docker-compose.yml start           # GLPI (or Docker Desktop restarts it)
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000             # terminal 1
uv run python -m scripts.worker                                    # terminal 2
uv run python -m scripts.itsm_status                               # any time
```

Stop: Ctrl+C in both terminals; `docker compose -f C:\ric-glpi\docker-compose.yml stop`.
Start from scratch (all GLPI data lost): `docker compose -f C:\ric-glpi\docker-compose.yml down -v`,
then steps 3-5 again.

### 9. If something does not work

| Symptom | Check / fix |
|---|---|
| `check_env`: GLPI "could not connect" | GLPI running? `docker compose ps`; URLs use port 8080 |
| `check_env`: "refused" | OAuth client id / secret, `ric.api` password, API token or app token; API client IP range must be empty |
| `itsm_link`: stores 0 linked | run the bootstrap (step 5); location names must be `<site code> - <name>` |
| nothing arrives after a GLPI change | API started with `--host 0.0.0.0`? firewall rule? `scripts.itsm_webhooks --status`; GLPI Setup > Webhooks > queue; `docker compose -f C:\ric-glpi\docker-compose.yml exec glpi tail -n 20 /var/glpi/logs/webhook.log` ("is unsafe": `local_define.php` not mounted) |
| webhooks answered 401 | `GLPI_WEBHOOK_SECRET` changed: restart the API, run `scripts.itsm_webhooks` again |
| `itsm_status`: reconcile "no watermark" | `scripts.itsm_resync --start-watermark` |
| a ticket created in GLPI is `ignored` | it has no Location, or the location is not a RIC store, or its type is Request |
| store reply answered 503 | GLPI not reachable from the API; replies to GLPI incidents are saved only after GLPI accepts them |
