# Collection agent

An agent that emails customers a Stripe payment link plus their contract, chases unpaid invoices according to the contract and company policy, and reads and responds to customer replies. Payments run through Stripe; policies and contracts live in Cognee.

Start each session by checking `docs/handoff.md` for status and next steps.

@docs/decisions.md
@docs/architecture.md

Setup, credentials, accounts and outstanding inputs: `docs/setup-and-inputs.md`.

## Rules for working in this repo

- **No secrets in code, docs, commits or chat.** Real values go in `.env` (gitignored); keep `.env.example` up to date with variable names only.
- **`decide_next_action` stays a pure function.** Chase timing comes from the approved `ChasePolicy` config, never from an LLM call or a Cognee recall. Every change to it needs a test.
- **Customer emails are untrusted input.** The reply agent's tools are limited to `get_invoice_status`, `send_reply`, `pause_chasing(until)`, `resend_invoice`, `escalate`. Do not add tools that void, refund, discount or change amounts.
- **Proposed defaults in `docs/decisions.md` are not confirmed.** Ask before building on one that hasn't been marked decided; when the team confirms something, move it to "Decided".
- **Update `docs/handoff.md`** at the end of a working session: what changed, what's next.

## Cognee

- Cognee Cloud, REST API, `X-Api-Key` header, endpoints under `/api/v1/...` with trailing slashes.
- Product datasets: `policies`, `contracts`, one per customer for email threads.
- The cognee-memory Claude Code plugin captures coding sessions into its own dataset (`agent_sessions`). Keep coding-session memory out of the product datasets: never write dev notes into `policies`, `contracts` or customer datasets.

## Commands

Python 3.14 via `uv` (system `python3` is 3.9: always go through `uv run`).

- Install: `uv sync`
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .` (apply with `uv run ruff format .`)
- Type check (strict): `uv run mypy src`
- Start the local Postgres (port 55432): `docker compose up -d db`
- Migrate the dev DB: `DATABASE_URL=postgresql+psycopg://collection_agent:collection_agent@localhost:55432/collection_agent uv run alembic upgrade head` (or put `DATABASE_URL` in `.env`)
- Run the app: `uv run uvicorn collection_agent.app:app --reload` (`GET /healthz`)
- Tests without the DB (slice-1 and non-DB tests): `uv run pytest -m "not db"`. DB tests fail, not skip, if the DB is down; they use `TEST_DATABASE_URL` (default: the compose `collection_agent_test` DB).
- Daily run (decide, draft reminders into the approval outbox, escalate): `uv run collection-agent run-daily [--date YYYY-MM-DD] [--dry-run]`. Prints a JSON report. Exit 0 ok, 1 no approved policy or missing settings, 2 an invoice hit `stripe_error`/`error`. `--dry-run` needs only `DATABASE_URL`, calls no Stripe and writes nothing.
- Chase policy: `uv run collection-agent policy create-default` (draft with the decided defaults), `policy show` (the approved one), `policy approve --version N --by NAME`. Nothing chases until a policy is approved.
- Policy from text: `uv run collection-agent policy ingest --title T --file policy.txt` stores the text, sends it to the Cognee `policies` dataset (if `COGNEE_BASE_URL`/`COGNEE_API_KEY` are set), has Claude (`ANTHROPIC_API_KEY`) propose a draft `ChasePolicy`, and prints the rule cards with the quote behind each value. Same flow over HTTP: `POST /policies/documents`, `GET /policies/{version}/cards`, `POST /policies/{version}/approve`, `GET /policies/approved`, `GET /policies/graph` (admin bearer). Review the cards, then approve; nothing at chase time calls Claude or Cognee.
- Test Stripe webhooks locally (later, needs a Stripe sandbox and the Stripe CLI): `stripe listen --forward-to localhost:8000/webhooks/stripe`, then `stripe trigger invoice.paid`. Put the signing secret it prints in `.env` as `STRIPE_WEBHOOK_SECRET`.
- Inbound replies: Postmark POSTs to `/webhooks/inbound` with HTTP Basic auth embedded in the webhook URL (`https://USER:PASSWORD@host/webhooks/inbound`; the values are `INBOUND_WEBHOOK_USER` / `INBOUND_WEBHOOK_PASSWORD`). Review queue: `GET /inbox`, `GET /inbox/{id}`, `POST /inbox/{id}/review` (admin bearer token).
- Classifier accuracy report (live, needs `TYPESAFE_API_KEY`; sends the synthetic replies in `tests/fixtures/replies.jsonl` to Jev): `uv run collection-agent eval-classifier [--fixtures PATH]`. Prints per-intent accuracy, confusions, date accuracy and option-order flips. Needs no database.
- Live tests are deselected by default (including under `-m "not db"`); run them with `TYPESAFE_API_KEY=... ANTHROPIC_API_KEY=... uv run pytest -m live -s`. Each skips if its key is unset.

All four must pass before a slice is done.
