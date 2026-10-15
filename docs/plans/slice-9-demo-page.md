# Slice 9 — One-page demo UI (+ demo-mode helpers)

Status: built and committed to `main` (15 Oct 2026).

## Goal

One page at `GET /demo` that drives the 60-second hackathon video. Everything goes through the real endpoints and real logic. There are three demo-only helpers, and they exist **only when `DEMO_MODE=true`** (default false; every `/demo/api/*` route and the page return 404 otherwise).

Storyboard it must support, in order:

1. Paste the policy text → rule cards appear, each with its source quote → **Approve**.
2. Create an invoice (with contract upload) → draft the invoice email → **Approve** in the queue (shows "sent").
3. **Skip 7 days** → the daily run drafts a reminder tagged with its rule (e.g. "No response for 7 days …") → approve it.
4. **Simulate customer reply** "We'll pay on the 12th" → goes through the real inbound flow (Jev → routing) → the timeline shows "promise to pay (confidence 0.9x)", "paused until …" and a drafted acknowledgement → approve it → sent.
5. Pay the Stripe invoice: the hosted invoice link opens Stripe's real page; pay with a test card. The `invoice.paid` webhook (via `stripe listen`) → the timeline shows "Paid", and the invoice is closed.

## Demo-mode helpers (`src/collection_agent/demo.py`)

- **Simulated clock.**
  - Table `demo_clock` (single row: `today` date, `updated_at`).
  - When `DEMO_MODE` is on, the existing `get_today` / `get_now` dependencies and the CLI's default date read it:
    - `today` = the stored date;
    - `now` = stored date + the real current London time of day.
  - When off, nothing changes. Test both.
  - `GET /demo/api/clock`, `POST /demo/api/clock` with `{"today": "YYYY-MM-DD"}`, and `POST /demo/api/clock/advance` with `{"days": 7}`. Advancing moves the clock forward by calendar days.
- **Run daily now.** `POST /demo/api/run-daily` runs the existing `run_daily` for the clock's date with the real Stripe gateway and email identity (same fail-closed rules as the CLI). Returns the report JSON.
- **Simulate a customer reply.** `POST /demo/api/simulate-reply` with `{"invoice_id": ..., "text": "...", "from_email": optional}`:
  - Builds a Postmark-inbound-shaped payload with:
    - `MailboxHash` = the invoice's reply token;
    - a fresh `Message-ID`;
    - `In-Reply-To` = the last sent outbox message for the invoice;
    - `Subject` = `Re: ` + its subject;
    - `From` = the customer's email unless overridden.
  - Passes it to **the same function** the webhook handler uses after auth (refactor if needed so there's one code path; the HTTP Basic check stays on the real webhook). Returns the inbound message id and route.
- **Email sender choice.** New setting `EMAIL_SENDER` = `postmark` (default) | `log`. `log` uses a `LoggingEmailSender`:
  - it records each "sent" email in a `demo_sent_emails` table (to, subject, body, attachment names, `created_at`);
  - it returns a fake provider id `log-<uuid>`.
  - It's allowed **only when `DEMO_MODE=true`**. Otherwise startup or the dependency raises a clear error. Real customers must never silently not get mail. `GET /demo/api/sent-emails` lists them.
- No other demo shortcuts.
  - **No "mark paid" button.** Paid comes only from Stripe.
  - No classifier fallback: if `TYPESAFE_API_KEY` is missing, the reply shows as `classification_failed` / needs review. That's the real behaviour.

## Timeline endpoint (real, admin)

`GET /invoices/{id}/timeline` → a chronological list of events derived from existing tables (no new audit table):
- invoice created;
- contract uploaded;
- each outbox row: drafted / approved / sent / rejected / failed, with `kind` and `rule_label`;
- each reminder recorded, with `rule_label`;
- each inbound message: received, `intent` + `confidence`, `route`, `review_status`;
- state facts from the invoice row: `promised_date` / `paused_until`, `disputed` / `with_human` / `escalated` with `escalation_rule_label`;
- Stripe events for the invoice (`invoice.paid`, …) from `stripe_events`.

Each event is `{at, type, title, detail, tags[]}`. Sort by `at`. For timestamps from the demo clock, use the stored values. Also `GET /invoices` (admin) without a `state` filter → recent invoices (id, reference, customer, amount, status, state, `due_on`), newest first. Keep the existing `?state=escalated` behaviour.

## The page (`src/collection_agent/static/demo.html` + `demo.js` + `demo.css`; served by FastAPI)

- No build step and no framework. Vanilla JS + CSS, with no CDN dependencies, so it works offline on stage. Plain, clean, legible at 1080p screen recording: large type, high contrast, one accent colour; light theme only is fine.
- **Auth:** on first load, a small dialog asks for the admin token. Keep it in `sessionStorage`. Every call sends `Authorization: Bearer`. Never put it in URLs.
- **Layout:** a three-column desktop grid.

  1. **Policy.** A textarea (pre-filled with `tests/fixtures/policy_sample.txt`, served from `/demo/api/sample-policy`) and an **"Extract rules"** button → `POST /policies/documents`.
     - Each rule card shows: label, value, source chip (`from document` / `default`), the quote in a small blockquote, and a "changed" badge.
     - A soft-guidance list.
     - An **Approve** button → `POST /policies/{v}/approve` (with the name "Demo reviewer").
     - A **"View Cognee graph"** button fetches `/policies/graph` with the token and shows it in a modal `<iframe sandbox="allow-scripts" srcdoc=...>`. It's hidden if that returns 503.
  2. **Invoice + timeline.**
     - A small "New invoice" form (customer name, email, reference, one line + amount, contract PDF file) → `POST /invoices`, then `PUT /invoices/{id}/contract`, then `POST /invoices/{id}/invoice-email`.
     - An invoice picker (`GET /invoices`).
     - The selected invoice's header: amount, due date, status/state pill, and a "Pay link" that opens `hosted_invoice_url` in a new tab.
     - The **timeline**: a vertical list with icons per type and rule tags as chips.
     - A **"Customer reply"** box (prefilled "Thanks for the reminder — we'll pay on the 12th.") → simulate-reply.
  3. **Approval queue + clock.**
     - The clock display, plus a **"Skip 7 days"** button (advance 7 → run-daily → refresh) and a "Run daily now" button.
     - The outbox (`GET /outbox`, pending first): each item shows kind, rule chip, to, subject, and an expandable body, with **Approve** / **Reject** buttons.
     - A "Sent mail" list when `EMAIL_SENDER=log`.
- **Refresh:** poll the selected invoice's timeline and the outbox every 3 s (so Stripe webhook updates appear without a click).
- **Error toasts:** show the endpoint's `error` code, never stack traces.
- **Accessibility basics:** buttons are `<button>`, inputs have labels, focus is visible.

## Docs

`docs/demo-runbook.md`, written by the builder, about one page:
- **Setup:** `.env` values needed, `DEMO_MODE=true`, `EMAIL_SENDER=log` or postmark, `stripe listen` command, start the app.
- **Picking dates:** set the clock to an early-month weekday so "Skip 7 days" lands on a 7-day `REMIND`, not `REMIND_PRE_DEADLINE`, and check with `run-daily --dry-run --date`. Example: clock Thu 1 Oct 2026 → due Mon 16 Nov 2026 → skip to Thu 8 Oct 2026 → `no_response`.
- **The click-path** matching the storyboard.
- **Reset:** a `collection-agent demo reset` CLI that deletes demo invoices/outbox/inbound/stripe_events/policies, but **only when `DEMO_MODE=true` and the DB name contains `collection_agent`**. It asks for typed confirmation unless `--yes`.

## Tests (DB ones marked `db`)

- Demo routes 404 when `DEMO_MODE` is off; the page 404s too.
- The clock: set/advance; `get_today`/`get_now` use it only in demo mode; the CLI default date uses it in demo mode.
- run-daily via the endpoint uses the clock date.
- simulate-reply:
  - goes through the shared inbound function (a fake classifier says `promise_to_pay`) → `paused_until` set and the ack drafted;
  - the threading headers are right;
  - unknown invoice → 404.
- `EMAIL_SENDER=log` outside demo mode → error; inside → approve records a `demo_sent_emails` row with the fake id.
- Timeline:
  - full storyboard sequence with fakes (policy approve → invoice → invoice email sent → advance clock → reminder drafted with rule tag → approve → simulated promise reply → ack approved → signed `invoice.paid` webhook) → assert the event types in order, the tags, and the final paid state.
- `GET /invoices` list.
- Reset CLI guards: refuses without demo mode, refuses with a wrong DB name, `--yes` works.
- Static files served; the page contains no inline secrets.
- Optional: a tiny JS smoke test is **not** required. Instead, the builder runs the app locally with fakes off and no keys, and loads `/demo` in a browser if one is available (DEMO_MODE on, `EMAIL_SENDER=log`). Note what renders: policy ingest will 503 without keys, which the page must show cleanly.
- All earlier tests pass.

## Done when

- `docker compose up -d db`, then all of these pass:
  - `uv run pytest`
  - `uv run pytest -m "not db"`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run alembic upgrade head` on the dev DB
- No network calls to external services. No real keys. `.env` untouched. Nothing committed.
