# Slice 3 — Stripe: `POST /invoices` and `POST /webhooks/stripe`

Status: built and committed to `main` (15 Oct 2026). Built with stripe SDK 16 on API version `2026-09-30.endive` (the SDK's own), not `dahlia` as written below.

## Goal

1. `POST /invoices` creates a Stripe customer (or reuses one) and a finalized Stripe invoice, then stores both. The response includes the `hosted_invoice_url`, which a later slice puts in the outbound email.
2. `POST /webhooks/stripe` verifies the signature, dedupes the event, and updates invoice status.

Tests never call Stripe. The API is faked behind a gateway interface, and webhooks are tested with fixture payloads signed in the test.

## Change to slice-1 logic: the stored `due_on` is the single deadline

Today `decide_next_action` recomputes `clear_by` from `sent_on`. Stripe's `due_date` is computed at invoice creation, and with the approval queue the email may go out days later, possibly in the next month. The two would disagree. `decisions.md` already says payment terms are stored as data at creation and never re-derived. So:
- Add `due_on: date` to `InvoiceSnapshot` (required).
- In `decide_next_action`, `clear_by = inv.due_on`. Remove the `policy.clearance_deadline(...)` call from it. `ChasePolicy.clearance_deadline` stays, and is used only at invoice creation.
- Update the slice-1 tests to pass `due_on` explicitly. Use the value the old computation gave, so every existing scenario keeps its dates and expected actions. Add one test showing that `clear_by` follows `due_on` even when it differs from what the policy would compute (`CLAUDE.md`: every change to this function needs a test).
- `load_decision_inputs` maps `invoices.due_on`.

## Out of scope

Sending email, attaching the contract PDF, the scheduler, Jev/Claude, the approval queue, Cognee. Setting `sent_on` stays with the email slice.

## Stripe facts to build on (from Stripe's own guidance, Oct 2026)

- Python SDK `stripe` latest (15.x), API version **`2026-08-26.dahlia`**. Always instantiate **`StripeClient`**. Never use the module-level `stripe.api_key` pattern.
- Never pass `payment_method_types`. The hosted invoice page shows the methods enabled in the Dashboard.
- Invoices: `collection_method="send_invoice"`, explicit `due_date` (unix timestamp), **`auto_advance=False`**. With `auto_advance` false Stripe doesn't advance, remind or email; we own chasing. Add invoice items with `invoice=<id>`, `amount`, `currency`, `description`. Then call **finalize** (not "send") to move the invoice to `open` and get `hosted_invoice_url`. Never call the send-invoice endpoint.
- Pass an idempotency key on every create/finalize call.
- Webhooks: verify the signature against the **raw request body** with the endpoint secret (SDK helper on `StripeClient`; check the exact method name in the installed SDK). Reject on failure. Events can be duplicated and arrive out of order.
- Use a restricted API key (`rk_…`) in real environments. The env var stays `STRIPE_SECRET_KEY`.

Check the installed SDK's own signatures rather than trusting this list from memory. If a call here doesn't exist in the installed SDK, use the SDK's equivalent and note it in your report.

## Settings

Add to `Settings`, all **optional** so the app and `/healthz` still start without them:
- `stripe_secret_key: str | None` (`STRIPE_SECRET_KEY`)
- `stripe_webhook_secret: str | None` (`STRIPE_WEBHOOK_SECRET`)
- `admin_api_token: str | None` (`ADMIN_API_TOKEN`), a new variable

Add `ADMIN_API_TOKEN=` to `.env.example` (name only) and to the env list in `docs/setup-and-inputs.md`, with a one-line comment saying it's the bearer token for internal endpoints. The endpoints **fail closed**: if a setting they need is missing they return 503 and do nothing.

## Schema changes (migration `0002`)

- `invoices.reference` — `String`, **unique, not null**. The caller's own reference, used for request idempotency and copied to Stripe metadata.
- `invoices.due_on` — `Date`, not null. The clearance deadline computed at creation, and the single deadline from then on (see above).
- `invoices.sent_on` becomes **nullable**. It's null until the email slice sends the invoice email.
- New table `stripe_events`: `id`, `created_at`, `event_id` (**unique**), `type`, `stripe_object_id` (nullable), `livemode` (bool).
- Repository follow-ups:
  - `list_open_invoice_ids` also requires `sent_on IS NOT NULL`. An unsent invoice is never chased.
  - `load_decision_inputs` raises a new `InvoiceNotSentError` when `sent_on` is null.
  - Add tests for both.

The migration must round-trip (`downgrade -1` / `upgrade head`) on an empty DB. Downgrade can assume no unsent invoices exist.

## Stripe gateway

`src/collection_agent/stripe_gateway.py`:
- `StripeGateway` `Protocol` with exactly what this slice needs, using plain types or small frozen dataclasses (no Stripe objects leak out):
  - `create_customer(name, email, idempotency_key) -> str` (customer id)
  - `create_invoice(customer_id, currency, due_date: int, metadata: dict[str, str], idempotency_key) -> str` (invoice id)
  - `add_invoice_item(invoice_id, customer_id, amount_minor, currency, description, idempotency_key) -> None`
  - `finalize_invoice(invoice_id, idempotency_key) -> FinalizedInvoice` (`id`, `status`, `hosted_invoice_url`)
- `LiveStripeGateway(api_key)` wraps one `StripeClient` built with `stripe_version="2026-08-26.dahlia"`.
- Tests use a `FakeStripeGateway` that records calls and **behaves like Stripe's idempotency keys**: the same key with the same parameters returns the stored result and creates nothing new; the same key with different parameters raises. It can also be told to raise a Stripe-style error on a chosen call.
- **One test of `LiveStripeGateway` itself** with the SDK's HTTP client replaced by a stub, so no network. Assert the outgoing requests contain `collection_method=send_invoice`, `auto_advance=false`, the `due_date`, the idempotency keys, no `payment_method_types`, and that nothing hits the send endpoint. If the SDK makes stubbing awkward, stub at the `httpx`/`requests` layer the SDK uses.

## `POST /invoices`

Auth: `Authorization: Bearer <ADMIN_API_TOKEN>`, compared with `secrets.compare_digest`. 401 if missing or wrong; 503 if `ADMIN_API_TOKEN` or `STRIPE_SECRET_KEY` isn't set.

Request (Pydantic):

```json
{
  "reference": "INV-2026-0042",
  "customer": {"name": "Acme Ltd", "email": "ap@acme.example"},
  "currency": "gbp",
  "lines": [{"description": "October retainer", "amount_minor": 250000}],
  "contract_id": "CTR-17"
}
```

- `reference`: 1–64 chars, `[A-Za-z0-9_-]`. `currency`: 3 lowercase letters. `lines`: 1–50 items, `amount_minor` > 0, `description` 1–500 chars. `contract_id`: optional, ≤ 64 chars. Email validated.
- Total `amount_minor` = sum of lines, computed in code.

Flow:
1. If an invoice with this `reference` exists: return **200** with it if the request matches what's stored (same customer email, currency and total), else **409** `{"error": "reference_conflict"}`. No Stripe calls either way.
2. `get_approved_policy` → if none, **409** `{"error": "no_approved_policy"}`.
3. `today` = current date in Europe/London (inject a clock so tests control it). `due_on = policy.clearance_deadline(today, uk_calendar([today.year, today.year + 1]))`. Stripe `due_date` = 23:59:59 Europe/London on `due_on`, as a unix timestamp.
4. Customer: reuse our `customers` row matched on case-insensitive email, else `create_customer` (idempotency key `"{reference}:customer"`) and insert the row.
5. `create_invoice` (key `"{reference}:invoice"`, metadata `{"reference", "contract_id"}` with `contract_id` omitted if null), then `add_invoice_item` per line (key `"{reference}:line:{i}"`), then `finalize_invoice` (key `"{reference}:finalize"`).
6. Insert the invoice row: `status=open`, `state=active`, `sent_on=NULL`, `due_on`, `hosted_invoice_url`, totals. Commit.
7. **201** with `{id, reference, stripe_invoice_id, hosted_invoice_url, amount_minor, currency, due_on, status}`.

Errors: any Stripe error → **502** `{"error": "stripe_error"}`, nothing written to the DB, and the log carries Stripe's request id but never the API key. Retrying the same request is safe, because Stripe idempotency keys return the same objects within 24h. Retry test: make the DB insert fail after `finalize` succeeded (e.g. patch the insert to raise once), retry the same request, then assert one invoice row **and** that the fake created exactly one customer, one invoice and one finalized invoice.

Concurrent duplicate: if two requests with the same `reference` race, the second hits the unique constraint at insert. Catch that `IntegrityError` and handle it like step 1 (200 if it matches, else 409), never a 500. Test it by pre-inserting the row between the existence check and the insert.

## `POST /webhooks/stripe`

- Read the **raw body**. Verify with `STRIPE_WEBHOOK_SECRET` and the `Stripe-Signature` header; bad or missing signature → **400**, nothing written. Missing secret → 503.
- In one DB transaction: insert into `stripe_events` with `ON CONFLICT (event_id) DO NOTHING`; if it was a duplicate → **200** with no further changes. Otherwise apply:

| Event | Effect on our invoice (matched by `stripe_invoice_id`) |
|---|---|
| `invoice.paid` | `status = paid` |
| `invoice.voided` | `status = void` |
| `invoice.marked_uncollectible` | `state = with_human` (a person decided to stop) |
| `invoice.payment_failed` | Recorded in `stripe_events` only |
| `invoice.overdue` | Recorded only. Our scheduler owns chasing |
| any other type | Recorded only |

- **Never move a terminal status backwards.** Once `paid` or `void`, later events don't change `status`, so out-of-order delivery is safe.
- Unknown `stripe_invoice_id` → recorded, **200** (could be an invoice created outside this service).
- If processing raises → roll back (the event row too) and return **500** so Stripe retries.
- No auth header beyond the signature.

## Fixtures and tests

- `tests/fixtures/stripe/`: one JSON event per handled type, plus one unhandled type. Shape them on API version `2026-08-26.dahlia` (`id`, `object: "event"`, `type`, `api_version`, `created`, `livemode: false`, `data.object` an invoice with `id`, `status`, `customer`, `metadata`, `hosted_invoice_url`). They're hand-written, because there's no Stripe account in this environment. Put a README in that folder saying they should be replaced with real ones captured via `stripe listen` / `stripe trigger` in a sandbox.
- A test helper signs a payload the way Stripe does (`t=<ts>,v1=<hex HMAC-SHA256(secret, f"{t}.{payload}")>`) with a test-only secret.

Required tests (DB tests marked `db`):
- `POST /invoices`: happy path (calls made in order, keys correct, row stored, `sent_on` null, `due_on` correct; e.g. today = 15 Oct 2026 → due Mon 16 Nov 2026); reuse of an existing customer by email in different case; same reference + same payload → 200 with no Stripe calls; same reference + different payload → 409; no approved policy → 409; Stripe error → 502 with nothing stored; retry after a failure between finalize and the DB insert → one invoice; 401 for missing or wrong token; 503 when the token isn't configured; validation 422s (bad currency, zero amount, no lines, bad reference).
- `due_date` timestamp is 23:59:59 London time in both BST and GMT cases.
- Webhooks: valid `invoice.paid` → paid; duplicate event → no second change, 200; bad signature → 400 with nothing stored; tampered body → 400; expired timestamp → 400 (SDK tolerance); `invoice.voided` → void; `marked_uncollectible` → with_human; `payment_failed` / `overdue` / unhandled → recorded, invoice unchanged; `invoice.voided` after `invoice.paid` → stays paid; unknown invoice → 200 recorded; missing secret → 503.
- `LiveStripeGateway` request-shape test (no network).
- Slice-1: the updated tests, plus the new `due_on` test.
- Migration `0002` round-trip; `list_open_invoice_ids` excludes unsent invoices; `load_decision_inputs` raises `InvoiceNotSentError`.
- All slice 1 and 2 tests still pass.

## Done when

- `docker compose up -d db`, then all pass: `uv run pytest`, `uv run pytest -m "not db"`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run alembic upgrade head` on the dev DB.
- No network calls to Stripe anywhere in tests. No real keys anywhere. `.env` is not created or edited.
- `CLAUDE.md` `## Commands` gains a line on testing webhooks locally later with `stripe listen --forward-to localhost:8000/webhooks/stripe`. Docs only; don't run it.
- Nothing committed.
- Report the exact SDK methods used for signature verification, invoice create, invoice item create and finalize.
