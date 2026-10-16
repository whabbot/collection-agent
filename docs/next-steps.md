# Next steps

Where the project stands after the hackathon (16 Oct 2026), and what to do next, in priority order. Status detail and the rehearsal log are in `docs/handoff.md`. Decisions are in `docs/decisions.md`.

## Where it stands

- Ten slices are built and pushed to `main`. 888 tests pass; ruff and mypy strict are clean.
- A full demo rehearsal passed end to end against real services (Stripe sandbox, Cognee Cloud, Claude Sonnet 5.5): policy → rule cards → invoice → reminder → promise to pay → paid via a Stripe webhook.
- Every outbound email is held for human approval. Mail was only logged (`EMAIL_SENDER=log`); nothing has been sent through Postmark yet.

## 1. Housekeeping (small, do first)

- **Rotate keys that touched chat or shared screens.** The Cognee API key was pasted into chat on 15 Oct.
- **Swap the Stripe key** in `.env` for a **restricted** sandbox key (`rk_test_…`) with write access to Customers, Invoices and Invoice items only. A full secret key is in use today.
- **Runbook fixes** in `docs/demo-runbook.md`:
  - `stripe listen` needs `--events invoice.paid,invoice.voided,invoice.marked_uncollectible,invoice.payment_failed,invoice.overdue`;
  - the Stripe CLI login expires after a few hours;
  - the CLI and `STRIPE_SECRET_KEY` must be on the same sandbox.
- **Cosmetic:** "View Cognee graph" shows even when Cognee isn't configured.
- **Rule-tag nit:** the `broken_promise_at_max` code is never produced. The label is right either way.
- **Starlette warning:** `TestClient` via `httpx` is deprecated (it suggests `httpx2`).
- **Stripe fixtures:** replace the hand-written ones in `tests/fixtures/stripe/` with real captures from `stripe listen`.

## 2. Decide the open questions (need the business, not code)

From `docs/decisions.md`:
- **Sender identity:** a named person or a team. The demo used placeholders (Accounts Team / Acme Co).
- **Who reviews the approval queue, and where:** email, Slack or an admin page.
- **Who receives escalations:** separately for disputes and extension requests.
- **Deadline month when sending is delayed:** `due_on` is fixed when the invoice is created. If approval delays the first email past a month end, should the deadline follow the sent month instead?
- Phrases the agent must never use.
- One-off invoices only, or recurring too? Rough monthly volume?
- If this is UK B2B: does statutory late-payment interest belong in the escalation policy? Needs proper legal advice.
- **Inputs still wanted:**
  - the real policy documents;
  - a sample contract;
  - 2–3 chase emails you'd be happy to send, and 1 you'd hate;
  - 10–20 real (anonymised) customer replies, to measure the classifier on real data.

## 3. Make it run for real

- **Hosting:** choose Render, Railway or Fly. It needs:
  - a web process (`uvicorn collection_agent.app:app`);
  - managed Postgres;
  - a weekday cron for `collection-agent run-daily` (proposed 08:00 UK);
  - secrets in the host's secret store.
- **Email (Postmark):**
  - a server token;
  - a confirmed sender signature on a billing subdomain, with SPF, DKIM, DMARC, and MX for inbound;
  - the inbound webhook URL with Basic auth.
  - New Postmark accounts may only send to their own domain until approved. Send to your own address first.
- **Stripe:** a webhook endpoint on the host, on the API version the SDK pins (`2026-09-30.endive`). Keep Stripe's own reminder emails off.
- **Policy:** ingest the real policy document, review the rule cards, approve.
- **Jev:** TypeSafe returned 402 (no credits) for the configured key. Either get credits onto that organisation and run `uv run collection-agent eval-classifier --classifier jev`, or stay on `CLASSIFIER=claude`. Claude scored 46/47 intents and 13/13 dates on the synthetic set.

## 4. Product work

- **Reviewer UI:** the approval queue is API-only apart from the demo page. Build it wherever reviewers will actually work.
- **Escalation notifications:** escalated invoices are only listed (`GET /invoices?state=escalated`); nobody is notified.
- **Claude-drafted reminders and replies:** they're templates today (decided for v1). Add Claude drafting once the tone examples exist, with Cognee soft guidance (tone, key-account exceptions) as context and every draft still approved by a person.
- **Contract questions:** they go to a person today. Answer them from contracts in Cognee, but only when a clause can be quoted.
- **Auto-send per intent:** log how reviewers act on each classification (the inbox already records `reviewer_intent`). Turn on auto-send only for intents with proven accuracy, likely "already paid" confirmations, resends and promise acknowledgements first. Disputes stay with a person.
- **Known gaps:**
  - two concurrent first invoices for one new email can create two Stripe customers;
  - a Stripe customer is single-currency;
  - contract-specific due dates could override the policy deadline at creation.

## How to keep building

The working method is in `docs/handoff.md` → Working method:
1. Write a spec in `docs/plans/`.
2. Hand it to a coding agent (Sonnet, medium effort).
3. Re-run the checks and read the risky code before committing to `main`.

Ask before building on anything still marked "proposed" in `docs/decisions.md`.
