# Collection agent — session handoff

Start here. This folder is the shared context for building the payment-collection agent.

## What we're building

An agent that:
1. Emails a customer a Stripe payment link plus their contract.
2. Chases the customer if they don't pay according to the contract.
3. Reads the customer's replies to chase emails and responds (or hands off to a human).

Policies (when to follow up, e.g. after 1 week of no response; company rules, e.g. clear by the 15th of the month) live in **Cognee**. Payments run through **Stripe**.

## Files

| File | What's in it |
|---|---|
| `CLAUDE.md` (repo root) | Read by Claude Code every session; imports decisions + architecture and sets the working rules |
| `docs/handoff.md` | This file: status and next steps |
| `docs/decisions.md` | What's decided, what's a proposed default awaiting confirmation, open questions |
| `docs/architecture.md` | The design: Stripe invoices, hard vs soft policy split, components, state machine, reply handling, guardrails |
| `docs/jev.md` | Jev (TypeSafe) reply-classifier integration notes: SDK, limits, what they change in the design |
| `docs/setup-and-inputs.md` | Accounts, credentials, inputs still needed, and how to re-create the Cognee setup in a new session |

## Status (15 Oct 2026)

- [x] Architecture designed (outbound chasing + inbound reply handling)
- [x] Cognee Cloud tenant created; API key verified working (tenant has one empty dataset, `default_dataset`)
- [x] `cognee-memory` Claude Code plugin installed (in the previous session's workspace only)
- [x] Stack, clearance month and `ChasePolicy` numbers confirmed (see `decisions.md` → Decided)
- [x] GitHub repo: `whabbot/collection-agent`
- [x] Cognee key in `~/.cognee/.env` verified working (200 on `/api/v1/datasets/`, 15 Oct)
- [x] **Slice 1 built** and committed to `main` (spec: `docs/plans/slice-1-chase-policy.md`): uv + Python 3.14 skeleton, `WorkingCalendar`/`uk_calendar`, `ChasePolicy`, `decide_next_action` + 56 tests; pytest, ruff, ruff format, mypy strict all pass
- [x] **Slice 2 built** and committed to `main` (spec: `docs/plans/slice-2-service-and-db.md`): FastAPI app with `/healthz`, Postgres 18 via `compose.yaml` (localhost:55432), SQLAlchemy 2 models, Alembic initial migration, repository layer mapping rows to the slice-1 models; 88 tests (58 without the DB)
- [x] **Slice 3 built** and committed to `main` (spec: `docs/plans/slice-3-stripe.md`): `POST /invoices` (bearer `ADMIN_API_TOKEN`; Stripe customer + finalized invoice, `auto_advance=false`, idempotency keys from `reference`) and `POST /webhooks/stripe` (signature check, event dedupe, paid/void/uncollectible). Stripe faked in tests; fixtures are hand-written. The stored `invoices.due_on` is now the single deadline `decide_next_action` uses
- [x] **Slice 4 built** and committed to `main` (spec: `docs/plans/slice-4-invoice-email.md`): Postmark sender (tracking off, no auto-retry), contract PDF upload, plain-text invoice email template, approval outbox (`GET /outbox`, approve, reject). Approving the invoice email sends it and sets `invoices.sent_on`, which starts the chase clock. A send with an unknown outcome stays in `sending` and blocks re-drafting
- [x] **Slice 5 built** and committed to `main` (spec: `docs/plans/slice-5-daily-run-and-reminders.md`): `collection-agent run-daily [--date] [--dry-run]` re-checks Stripe, drafts template reminders (friendly → firm → formal) into the outbox, or escalates (`GET /invoices?state=escalated`). Approving a reminder records it in `reminders`. `POST /outbox/{id}/resolve` clears stuck sends. `collection-agent policy create-default|approve|show` seeds the first `ChasePolicy`
- [x] **Slice 6 built** and committed to `main` (spec: `docs/plans/slice-6-inbound-replies.md`): `POST /webhooks/inbound` (Basic auth, Message-ID dedupe, auto-reply drop, matching by reply token → threading → sender), `JevClassifier` (typesafe-sdk 0.7.5, pinned `jev-1.13.0`, date parts assembled in code), code routing table with template reply drafts in the outbox, `GET /inbox` + review logging. 47 synthetic replies (5 injection cases); live eval via `collection-agent eval-classifier` / `pytest -m live`, not yet run
- [x] **Slice 7 built** (spec: `docs/plans/slice-7-rule-tags.md`): `describe_rule` tags each reminder draft, sent reminder and escalation with the rule that fired (e.g. "No response for 7 days (last contact …)"); shown in outbox and escalation responses and daily-run outcomes
- [ ] Confirm remaining proposed defaults (email provider, hosting, scheduler, promise-date limit, reply routing, taste)
- [ ] Collect inputs: policy docs, sample contract, example chase emails, sample customer replies
- [ ] Stripe test-mode keys + webhook secret
- [ ] Email provider account + domain DNS
- [ ] Rotate the Cognee API key (it was pasted into chat on 15 Oct) — current key works; rotation not confirmed

## Working method

The main session orchestrates: it writes a spec in `docs/plans/`, then hands the build to a coding subagent (**Sonnet, medium effort**), then re-runs the checks itself before reporting. Work is committed straight to `main` — no feature branches or PRs.

## Next steps

1. **Before the first real run:** `uv run collection-agent policy create-default`, then `policy approve --version 1 --by <name>`. Choose where the daily job runs (hosting is still only proposed) and call `collection-agent run-daily` weekday mornings (proposed 08:00 UK).
2. **Run the live Jev eval** once `TYPESAFE_API_KEY` is in `.env`: `uv run collection-agent eval-classifier`. Check per-intent accuracy and the option-order flips before trusting `CLASSIFIER_MIN_CONFIDENCE=0.6`.
3. When policy docs arrive: load them into the Cognee `policies` dataset and test recall before deciding how much to extract into typed config.
4. **First live run (user wants a Stripe test account):**
   - The user creates a Stripe **sandbox** and a restricted key, and puts it in `.env` as `STRIPE_SECRET_KEY`. Claude can't create accounts or enter keys.
   - Run the app locally, use `stripe listen --forward-to localhost:8000/webhooks/stripe` for the webhook secret, then create a test invoice end to end.
   - Replace the hand-written fixtures in `tests/fixtures/stripe/` with real captures.
   - For email: a Postmark server token, a confirmed sender signature on the billing subdomain, and `EMAIL_FROM_*` / `REPLY_TO_DOMAIN` / `COMPANY_NAME` in `.env`. Send only to your own address first.
5. **Later:** Claude drafting of reminders, once tone examples and policy docs exist (templates are the v1 decision).

## Open points

- **Deadline month when sending is delayed:** `due_on` is fixed at invoice creation (15th of the month after creation). If approval delays the first email past a month end, should the deadline follow the creation month (current behaviour) or the sent month?
- **Contract-specific due dates:** `due_on` is now the single deadline, so a contract date can override it at creation once we want that.
- **Customers by email:** two concurrent first invoices for the same new email can create two Stripe customers. There's no unique constraint on email, because different companies might share an AP inbox. Revisit if it bites.
- **Multi-currency:** a Stripe customer is single-currency. Invoicing an existing customer in a new currency will fail with 502.
- **Escalation notifications:** escalated invoices are only listed by `GET /invoices?state=escalated`; nobody is notified. Who receives escalations is still an open question.
- **Reviewer UI:** the outbox is API-only. Where the approval queue lives (email, Slack, admin page) is still open.
- **Sender identity** (person vs team) is still open. It's set through `EMAIL_FROM_NAME`, so no code change is needed when it's decided.
- **Rule tag nit:** `broken_promise_at_max` is unreachable (the spec's condition keys on reminder count). It should be chosen when the promise is broken at max reminders. The label is the same either way.
- Housekeeping: Starlette warns that `TestClient` via `httpx` is deprecated (suggests `httpx2`).
