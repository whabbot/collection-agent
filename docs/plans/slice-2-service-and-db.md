# Slice 2 — FastAPI service skeleton + Postgres persistence

Status: built and committed to `main` (15 Oct 2026).

## Goal

A runnable FastAPI app with a health check, and a Postgres schema (SQLAlchemy 2 + Alembic) that stores everything `decide_next_action` needs. Plus a repository layer that turns rows into the slice-1 input models, so the scheduler (later slice) can do `load → decide_next_action → record`.

## Scope

In:
- Settings, DB engine/session, SQLAlchemy models, Alembic migration, repository functions, FastAPI app with `GET /healthz`, local Postgres via Docker Compose, tests.

Out (later slices): Stripe, email sending/inbound webhooks, Jev/Claude, the daily scheduler, `POST /invoices`, approval queue, auth, Cognee. Don't add those dependencies.

Don't change slice-1 behaviour. `chasing.py`, `policy.py` and `calendar.py` stay as they are, apart from imports if needed.

## Stack

Latest stable versions via `uv add` (no hand-pinned old versions):
- `fastapi`, `uvicorn[standard]`
- `sqlalchemy` 2.x (**sync** engine and sessions; volume is low and the scheduler is a batch job), `psycopg[binary]` 3 (URL scheme `postgresql+psycopg://`)
- `alembic`
- `pydantic-settings`
- Dev: `httpx` (FastAPI `TestClient`)

## Local Postgres

- `compose.yaml` at the repo root: service `db`, image `postgres:18`, host port **55432** → 5432 (avoids clashing with any local Postgres), named volume, healthcheck (`pg_isready`).
- Dev-only credentials for this local container: user `collection_agent`, password `collection_agent`, database `collection_agent`. Add an init script (mounted into `/docker-entrypoint-initdb.d/`) that also creates `collection_agent_test`. These are throwaway local values, not secrets. Real `DATABASE_URL`s only ever go in `.env` or host secrets.
- `.env.example` keeps variable names only (no change needed; `DATABASE_URL` is already there).

## Settings

`src/collection_agent/settings.py`: `Settings(BaseSettings)` with `database_url: str` (env `DATABASE_URL`, reads `.env` if present). Expose `get_settings()` (cached). Nothing else yet. Don't add settings for keys that later slices will need.

## Database layer

`src/collection_agent/db/`:
- `engine.py`: `make_engine(url)`, `make_session_factory(engine)`. No engine created at import time.
- `models.py`: SQLAlchemy 2 typed declarative models (`Mapped[...]`, `mapped_column`). Use UUID primary keys, `timestamptz` for timestamps (`server_default=now()`), and Postgres enums (or `String` + check constraint) for the enum columns. Reuse the slice-1 `StrEnum`s for the values.

Tables:

| Table | Columns (beyond `id`, `created_at`) | Constraints |
|---|---|---|
| `customers` | `stripe_customer_id` (unique), `name`, `email` | |
| `invoices` | `customer_id` FK, `stripe_invoice_id` (unique), `amount_minor` (int, > 0), `currency` (char 3), `hosted_invoice_url` (nullable), `contract_id` (nullable), `sent_on` (date), `status` (`InvoiceStatus`), `state` (`InvoiceState`), `paused_until` (date, null), `promised_date` (date, null), `reply_token` (unique, random, for `reply+<token>@…` addresses), `updated_at` | index on `(status, state)` |
| `reminders` | `invoice_id` FK, `kind` (`ReminderKind`), `sent_on` (date) | index on `invoice_id` |
| `inbound_messages` | `invoice_id` FK **nullable** (unmatched replies go to a human), `message_id` (**unique**, so duplicate webhooks are deduped), `from_address`, `subject`, `body_text`, `received_at` (timestamptz), `counts_as_response` (bool; false for out-of-office/auto-replies) | index on `invoice_id` |
| `chase_policies` | `version` (int, unique), the six `ChasePolicy` fields, `status` (`draft` / `approved` / `superseded`), `approved_by` (null), `approved_at` (null) | **partial unique index**: at most one row with `status = 'approved'`; check constraints mirroring `ChasePolicy`'s validation |

Money is stored only as integer minor units plus currency. No floats.

## Alembic

- `alembic.ini` + `migrations/` at the repo root. `env.py` reads the URL from `Settings` (or `-x url=` override), and `target_metadata` is the models' metadata.
- One hand-checked initial migration creating everything above. Autogenerate it, then read it and fix it (enums, partial index and check constraints must be in it).
- `uv run alembic upgrade head` must work against the compose DB.

## Repository functions

`src/collection_agent/db/repository.py`, taking a `Session`. Pure data access, no business rules beyond what's stated:

- `load_decision_inputs(session, invoice_id) -> DecisionInputs`, a frozen dataclass holding `InvoiceSnapshot`, `list[ReminderSent]`, `list[CustomerReply]`.
  - Replies = inbound messages for the invoice with `counts_as_response = true`. `received_on` = `received_at` converted to **Europe/London** local date (`zoneinfo`). Test the 23:30 UTC case in BST (lands on the next day).
  - Raises a clear error if the invoice doesn't exist.
- `list_open_invoice_ids(session) -> list[UUID]`: `status = open` and `state = active`.
- `record_reminder(session, invoice_id, kind, sent_on) -> None`.
- `record_inbound_message(session, ...) -> bool`: inserts. Returns `False`, without raising, if `message_id` already exists (dedupe). Use `INSERT … ON CONFLICT DO NOTHING`.
- `get_approved_policy(session) -> ChasePolicy`: the single approved row mapped to the slice-1 `ChasePolicy`. Raises a clear error if none is approved. The scheduler must never fall back to defaults silently.
- `create_draft_policy(session, policy: ChasePolicy) -> int` (returns the new version, max+1).
- `approve_policy(session, version, approved_by) -> None`: in one transaction, sets the current approved row (if any) to `superseded` and this one to `approved` with `approved_by`/`approved_at`. Only a `draft` can be approved.

Callers own commit/rollback; repository functions don't commit.

## FastAPI app

`src/collection_agent/app.py`: `create_app()` factory plus a module-level `app = create_app()` for uvicorn. Session dependency built from settings. **`GET /healthz`** runs `SELECT 1` and returns `{"status": "ok"}`, or 503 with `{"status": "db_unavailable"}` if the DB is unreachable. No other routes yet.

Running it: `uv run uvicorn collection_agent.app:app --reload`.

## Tests

- DB tests run against the compose Postgres test database. URL comes from env `TEST_DATABASE_URL`, defaulting to the local compose test DB on port 55432.
- Session-scoped fixture: run Alembic `upgrade head` on the test DB (so the migration itself is tested), dropping and recreating the schema first. Each test runs in a transaction (or nested savepoint) that is rolled back.
- If the test DB is unreachable, the DB tests **fail** with a message telling you to run `docker compose up -d db`. They must not silently skip. Slice-1 tests must still run without Docker. Mark DB tests (e.g. a `db` marker) so `uv run pytest -m "not db"` works.

Required tests:
- Migration: upgrade head on an empty DB succeeds; `alembic downgrade base` then `upgrade head` round-trips.
- Constraints: duplicate `stripe_invoice_id` rejected; second `approved` policy rejected at DB level; `amount_minor <= 0` rejected; invalid policy numbers rejected by check constraints.
- `load_decision_inputs`: maps all fields; excludes `counts_as_response = false`; London-date conversion (BST case 23:30 UTC → next day; GMT case in January → same day); unknown invoice raises.
- `list_open_invoice_ids`: excludes paid, void, disputed, with_human and escalated.
- `record_inbound_message`: second insert with the same `message_id` returns `False` and leaves one row.
- Policy lifecycle: create draft → approve → `get_approved_policy` returns it; approving v2 supersedes v1; approving a non-draft raises; no approved policy → `get_approved_policy` raises.
- End-to-end: seed an invoice + reminders + reply, then `load_decision_inputs` → `decide_next_action` gives the expected action (reuse a slice-1 scenario's dates).
- `/healthz`: 200 with the DB up; 503 when the session dependency is overridden with an unreachable URL.

## Done when

- `docker compose up -d db` then all pass: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src` (strict, including `src/collection_agent/db`).
- `uv run alembic upgrade head` works against the dev DB, and `uv run uvicorn collection_agent.app:app` serves `/healthz` → 200.
- `CLAUDE.md` `## Commands` updated with: start DB, migrate, run app, run tests (all, and without DB).
- No secrets, no network calls beyond package downloads and the local Docker DB, no Cognee writes. Nothing committed.
