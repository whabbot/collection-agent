# Slice 4 — First invoice email via Postmark, through an approval outbox

Status: built and committed to `main` (15 Oct 2026). Deviation: a send with an unknown outcome (timeout, 5xx) stays in `sending` instead of `failed`, so it can't be re-drafted and sent twice.

## Goal

After `POST /invoices`, a person uploads the contract PDF, asks for the invoice email to be drafted, reviews it, and approves it. Approval sends it through Postmark, and **sets `invoices.sent_on`**, which starts the chase clock. Every outbound email goes through an **outbox with human approval** (decided: approval queue first). This slice builds that outbox in its general form, so reminder emails can reuse it later.

## Out of scope

Reminder drafting (LLM), the daily scheduler, inbound email/reply handling (`/webhooks/inbound`), Jev, Cognee, a reviewer UI (API only for now), HTML email.

## Decisions this slice builds on

- Postmark for sending (decided). Plain-text emails (proposed "taste" default; it's the only format built here).
- **Fixed template, no LLM**, for the first invoice email. Wording is below. The reviewer sees the exact text before it goes.
- Sender identity is still an open question (person vs team), so it comes from settings, not code.
- Replies must land on the per-invoice address `reply+<reply_token>@<REPLY_TO_DOMAIN>` (architecture: inbound mapping).

## Settings (all optional; endpoints that need them fail closed with 503)

- `postmark_server_token` (`POSTMARK_SERVER_TOKEN`, already in `.env.example`)
- `email_from_address` (`EMAIL_FROM_ADDRESS`), e.g. a confirmed Postmark sender signature on the billing subdomain
- `email_from_name` (`EMAIL_FROM_NAME`)
- `reply_to_domain` (`REPLY_TO_DOMAIN`), e.g. `billing.example.com`
- `company_name` (`COMPANY_NAME`)
- `postmark_message_stream` (`POSTMARK_MESSAGE_STREAM`), default `"outbound"`

Add the new names to `.env.example` (names only) and to the env list in `docs/setup-and-inputs.md`, one short comment each.

## Schema (migration `0003`)

`contracts` — one current contract PDF per invoice:
- `id`, `created_at`, `invoice_id` FK (**unique**), `filename`, `content` (`LargeBinary`/bytea), `sha256`, `size_bytes`.
- Replacing a contract is allowed only while no invoice email has been **sent** for that invoice.

`outbound_emails` — the outbox:
- `id`, `created_at`, `updated_at`, `invoice_id` FK, `kind`
  - `kind` values: `invoice` for now. Reminder kinds will be added later; store as a Postgres enum or string with a check, and make it extendable.
- `to_address`, `from_address`, `from_name`, `reply_to`, `subject`, `body_text`
- `attach_contract` (bool)
- `status`: `pending_approval` → `sending` → `sent`; or `rejected`; or `failed`.
- `approved_by`, `approved_at`, `rejected_by`, `rejected_at`, `reject_reason`, `sent_at`, `provider_message_id`, `error` (all nullable).
- **Partial unique index**: at most one `invoice`-kind email per invoice whose status is not `rejected`/`failed`. This is the guard against sending the invoice email twice.
- Index on `status`.

The migration must round-trip.

## Email sender

`src/collection_agent/email_sender.py`:
- `EmailSender` `Protocol`: `send(message: OutgoingEmail) -> SentEmail` (`provider_message_id`). `OutgoingEmail` is a frozen dataclass: from (name + address), to, reply_to, subject, text_body, attachments (`name`, `content: bytes`, `content_type`), metadata (`dict[str, str]`).
- `PostmarkSender(server_token, message_stream, http_client)` posts to `https://api.postmarkapp.com/email`:
  - headers `Accept`/`Content-Type: application/json`, `X-Postmark-Server-Token`;
  - body `From` (`"Name <addr>"`, properly quoted), `To`, `ReplyTo`, `Subject`, `TextBody`, `MessageStream`;
  - `Attachments` (`Name`, base64 `Content`, `ContentType`);
  - `Metadata` (`invoice_id`, `outbound_email_id`);
  - **`TrackOpens: false`, `TrackLinks: "None"`**, so the Stripe payment link is never rewritten.
  - Success is HTTP 200 with `ErrorCode == 0`; take `MessageID` from the response. Anything else raises `EmailSendError` carrying Postmark's `ErrorCode`, never the token.
  - Use `httpx` (move it from dev to runtime dependencies), timeout 10 s, **no automatic retries**. A retried send could duplicate an email to a customer.
- Tests: `FakeEmailSender` records messages and can be told to fail. One `PostmarkSender` test uses `httpx.MockTransport` (no network) and asserts the exact request: URL, token header, JSON fields, tracking off, base64 attachment. Plus error handling for HTTP 422 with an `ErrorCode`.

## Template (plain text)

`src/collection_agent/templates.py` (plain Python string formatting, no template engine), `render_invoice_email(...) -> (subject, body)`:

Subject: `Invoice {reference} from {company_name}`

```
Hello {customer_name},

Please find attached your contract{contract_ref}. Invoice {reference} for {amount} is due by {due_date}.

You can view and pay it securely here:
{hosted_invoice_url}

If you have any questions, just reply to this email.

Thank you,
{from_name}
{company_name}
```

- `{contract_ref}` = ` ({contract_id})` when `contract_id` is set, else empty.
- `{due_date}` like `Monday 16 November 2026`. Build it in code; no locale dependence.
- `{amount}`: format from minor units with a small helper. 2-decimal currencies use a symbol for `gbp` (£), `eur` (€) and `usd` ($), with thousands separators: `£2,500.00`. Other 2-decimal currencies render as `2,500.00 CHF`. Stripe's zero-decimal currencies (`jpy`, `krw`, `vnd`, and the rest of Stripe's documented list) get no decimals. Test each branch.
- Refuse to render if `hosted_invoice_url` is missing.

## Endpoints (all require `Authorization: Bearer <ADMIN_API_TOKEN>`, reusing the existing admin dependency)

1. **`PUT /invoices/{invoice_id}/contract`**: raw PDF body (`Content-Type: application/pdf`); filename from a `filename` query param (sanitised, must end `.pdf`).
   - Rejects with 422: an empty body, more than **7 MB** (base64 inflates by ~37%, and Postmark's limit is 10 MB per message), or content not starting with `%PDF-`.
   - 404 for an unknown invoice. 409 if an invoice email for it has already been sent.
   - Replaces any existing contract. 200 with `{filename, size_bytes, sha256}`.
2. **`POST /invoices/{invoice_id}/invoice-email`**: renders the template into a `pending_approval` outbox row (`attach_contract=true`). From/Reply-To come from settings. `reply_to = reply+{reply_token}@{reply_to_domain}`.
   - 404 for an unknown invoice.
   - 409 `{"error": "contract_missing"}` if there's no contract.
   - 409 `{"error": "invoice_not_open"}` unless the invoice is `open`/`active`.
   - 409 `{"error": "already_drafted"}` if a non-rejected/non-failed invoice email exists. Catch the unique-index race too.
   - 503 if email settings are missing.
   - 201 with the outbox row.
3. **`GET /outbox`** with optional `status` filter (default `pending_approval`): newest first. Returns rows without attachment bytes, plus the contract filename/size when `attach_contract`.
4. **`GET /outbox/{id}`**: one row, same shape.
5. **`POST /outbox/{id}/approve`** with body `{"approved_by": "<name>"}` (1–100 chars):
   1. In one transaction, lock the row (`FOR UPDATE`). It must be `pending_approval`, else 409 `{"error": "not_pending"}`. Re-check that the invoice is still `open`/`active`, else mark the row `rejected` with `reject_reason="invoice no longer open"` and return 409 `{"error": "invoice_not_open"}`. Set `status=sending`, `approved_by` and `approved_at`. Commit.
   2. Send via the `EmailSender` (attach the contract if `attach_contract`). Pass the outbox id and invoice id as metadata.
   3. On success, in one transaction: `status=sent`, `sent_at`, `provider_message_id`; for kind `invoice`, set `invoices.sent_on` = today in Europe/London (inject the clock), but only if `sent_on` is null. 200 with the row.
   4. On `EmailSendError`: `status=failed`, `error` = a short reason without secrets; 502 `{"error": "send_failed"}`. A failed invoice email can be re-drafted by calling endpoint 2 again.
   5. If the process dies between steps 1 and 3, the row stays in `sending`. Never auto-resend it, because it may have gone out. Document this in a code comment; a person checks Postmark's activity log.
   6. 503 if Postmark or email settings are missing. Check this **before** step 1, so nothing changes state.
6. **`POST /outbox/{id}/reject`** with `{"rejected_by": "...", "reason": "..."}`: only from `pending_approval`, else 409. Sets `rejected`.

## Tests (DB ones marked `db`)

- Template: exact rendered text for a GBP invoice with a contract id; no contract id; zero-decimal currency; non-symbol currency; missing URL raises; due date formatting.
- Contract upload: happy path; replace; empty, oversize, non-PDF and bad filename → 422; unknown invoice → 404; after send → 409; auth 401/503.
- Draft: happy path (subject/body/reply_to exact, `pending_approval`); no contract → 409; invoice paid → 409; second draft → 409; after a reject, re-draft works; settings missing → 503.
- Approve: happy path (Fake sender got the exact message with attachment bytes and metadata; row `sent`; `invoices.sent_on` = injected today; the invoice now appears in `list_open_invoice_ids`); approve twice → second is 409 and only one send; invoice paid between draft and approve → 409, row `rejected`, no send; sender failure → 502, row `failed`, `sent_on` still null, re-draft allowed; Postmark settings missing → 503 and the row is still `pending_approval`.
- Reject: happy path; rejecting a sent row → 409.
- Outbox listing: status filter, ordering, no attachment bytes in the response.
- `PostmarkSender` request shape via `httpx.MockTransport`; 422 error path; the token never appears in the exception text.
- Migration `0003` round-trips; the partial unique index blocks a second active invoice email.
- All earlier tests still pass.

## Done when

- With `docker compose up -d db` running, all of these pass:
  - `uv run pytest`
  - `uv run pytest -m "not db"`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run alembic upgrade head` on the dev DB
- No network calls to Postmark or Stripe in tests. No real tokens anywhere. `.env` is not created or edited.
- Nothing committed.
