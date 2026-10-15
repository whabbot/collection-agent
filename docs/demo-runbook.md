# Demo runbook

The demo page is `GET /demo`. It exists only when `DEMO_MODE=true`; otherwise it and every `/demo/api/*` route return 404. Everything on the page goes through the real endpoints and the real logic. The only helpers are a simulated clock, "run daily now" and "simulate a customer reply". There is no "mark paid": paid comes only from Stripe.

## Setup

1. `docker compose up -d db`, then `DATABASE_URL=... uv run alembic upgrade head`.
2. `.env` (names only here; values stay in `.env`): `DATABASE_URL`, `ADMIN_API_TOKEN`, `DEMO_MODE=true`, `STRIPE_SECRET_KEY` (test mode), `STRIPE_WEBHOOK_SECRET`, `EMAIL_FROM_ADDRESS`, `EMAIL_FROM_NAME`, `REPLY_TO_DOMAIN`, `COMPANY_NAME`, `TYPESAFE_API_KEY` (Jev), `ANTHROPIC_API_KEY` (rule extraction), `COGNEE_BASE_URL` and `COGNEE_API_KEY` (policy ingest, graph).
3. Email: `EMAIL_SENDER=log` records "sent" mail in the page's Sent mail list and sends nothing. `EMAIL_SENDER=postmark` (default) also needs `POSTMARK_SERVER_TOKEN`. The `log` sender is refused unless `DEMO_MODE=true`.
4. Forward Stripe events: `stripe listen --forward-to localhost:8000/webhooks/stripe`, and put the `whsec_...` it prints in `STRIPE_WEBHOOK_SECRET`.
5. Start the app: `uv run uvicorn collection_agent.app:app --port 8000`, open `http://localhost:8000/demo`, paste the admin token when asked (kept in the tab's `sessionStorage` only).

Without a Jev key a simulated reply shows as `classification_failed` / needs review, and without Anthropic or Cognee keys "Extract rules" shows the endpoint's error. That is the real behaviour; nothing falls back.

## Picking dates

The clock starts at the real London date. Set it so "Skip 7 days" lands on the 7-day rule (`no_response`), not the pre-deadline reminder, and on a working day:

- Set the clock to an early-month weekday: `POST /demo/api/clock {"today": "2026-10-01"}` (Thu 1 Oct 2026). With the sample policy the invoice is due Mon 16 Nov 2026 (15th of the following month, rolled to a working day).
- "Skip 7 days" moves it to Thu 8 Oct 2026: `no_response`.
- Check before recording: `uv run collection-agent run-daily --dry-run --date 2026-10-08` (with an invoice sent on the clock date).

## Click path (60 seconds)

1. Policy column: Extract rules, read the cards and their source quotes, Approve. Optional: View Cognee graph (hidden if Cognee is not configured).
2. Invoice column: New invoice (customer, reference, one line, contract PDF), Create and draft email. In the queue, Approve the invoice email: the timeline shows it sent.
3. Skip 7 days. The daily run drafts a reminder tagged "No response for 7 days ...". Approve it.
4. Send as customer: "Thanks for the reminder - we'll pay on the 12th." The timeline shows the promise to pay with its confidence, "paused until ...", and a drafted acknowledgement. Approve it.
5. Pay link opens Stripe's hosted page; pay with a test card (4242 4242 4242 4242). The `invoice.paid` webhook arrives through `stripe listen`; the timeline shows "Paid in Stripe" and the invoice is paid.

The page polls the timeline and queue every 3 seconds, so step 5 appears without a click.

## Reset

`uv run collection-agent demo reset` deletes the demo invoices, customers, contracts, reminders, outbox, inbound messages, Stripe events, policies and policy documents, daily runs and logged mail. It refuses unless `DEMO_MODE=true` and the database name contains `collection_agent`, and asks you to type `reset` unless you pass `--yes`. The simulated clock is left as it is; set it again from the API.
