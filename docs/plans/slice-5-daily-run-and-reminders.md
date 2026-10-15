# Slice 5 — Daily run, reminder drafts, escalation

Status: built and committed to `main` (15 Oct 2026).

## Goal

A daily job goes through every open, sent invoice:
1. It asks `decide_next_action` what's due.
2. It re-checks the invoice in Stripe.
3. It either drafts a reminder into the slice-4 outbox (for human approval) or escalates the invoice to a human.

Approving a reminder sends it and records it in `reminders`, which feeds the next decision. Also included: the CLI to seed and approve the first `ChasePolicy`, and the missing endpoint to resolve outbox rows stuck in `sending`.

## Decided for this slice

- **Reminders come from fixed templates (no LLM)**, decided 15 Oct 2026. Claude drafting comes later, once tone examples and policy docs exist. Every draft still needs human approval.
- Tone escalates with the reminder number: friendly → firm → formal. No legal threats and no "final notice" wording, ever, in these templates.

## Out of scope

LLM drafting, Cognee, inbound email/replies, Jev, hosting or cron setup (the job is a CLI command; whatever host is chosen will call it), notifications for escalations (open question who receives them; for now they're listed by an endpoint).

## Reminder kinds in the outbox

Extend `outbound_emails.kind` (migration `0004`): `invoice`, `reminder`, `reminder_pre_deadline`, `reminder_broken_promise`. Mapping to slice-1:

| `Action` | outbox `kind` | `ReminderKind` recorded on send |
|---|---|---|
| `REMIND` | `reminder` | `REMINDER` |
| `REMIND_PRE_DEADLINE` | `reminder_pre_deadline` | `PRE_DEADLINE` |
| `REMIND_BROKEN_PROMISE` | `reminder_broken_promise` | `BROKEN_PROMISE` |

New **partial unique index**: at most one reminder-kind row per invoice with status `pending_approval` or `sending`. This is the guard that stops the daily run from drafting a second reminder while one is waiting for approval.

## Templates (plain text, `templates.py`)

`render_reminder_email(kind, level, facts) -> (subject, body)`.
- `level` = number of reminders already sent + 1, clamped to 1–3.
- `facts`: customer name, reference, amount (minor units + currency, formatted with the existing helper), `due_on`, `today`, hosted URL, `promised_date` (broken promise only), from name, company name.
- **Dates are always absolute** (existing `format_date`), never "in 3 days". A draft can wait days for approval.
- `due_phrase`:
  - `today <= due_on` → `is due by {due_date}`
  - else → `was due on {due_date} and is now overdue`

Opening line by kind:

| Kind | Opening line |
|---|---|
| `reminder` | `Invoice {reference} for {amount} {due_phrase}, and we haven't yet received payment.` |
| `reminder_pre_deadline` | `This is a reminder that invoice {reference} for {amount} is due by {due_date}.` |
| `reminder_broken_promise` | `You let us know you expected to pay invoice {reference} for {amount} by {promised_date}, but we haven't yet received payment. It {due_phrase}.` |

Wrapper by level:

**Level 1 (friendly).** Subject: `Reminder: invoice {reference} from {company_name}`

```
Hello {customer_name},

{opening}

You can view and pay it here:
{hosted_invoice_url}

If you've already paid, thank you, and please ignore this email. If anything is unclear, just reply.

Thanks,
{from_name}
{company_name}
```

**Level 2 (firm).** Subject: `Second reminder: invoice {reference}`

```
Hello {customer_name},

{opening}

Please arrange payment using the link below:
{hosted_invoice_url}

If there's a problem with the invoice, please reply so we can sort it out.

Regards,
{from_name}
{company_name}
```

**Level 3 (formal).** Subject: `Invoice {reference}: payment required`

```
Dear {customer_name},

{opening}

Please arrange payment promptly using the link below:
{hosted_invoice_url}

If we don't hear from you, this will be passed to our accounts team to follow up. If you believe this invoice is incorrect, please reply and tell us why.

Yours sincerely,
{from_name}
{company_name}
```

Tests check the exact text for every kind × level, plus both `due_phrase` branches. Also add a test asserting that no rendered output contains `final notice`, `legal`, `court`, `solicitor`, `debt collect` or `interest` (case-insensitive).

## Stripe re-check

Add `get_invoice_status(stripe_invoice_id) -> str` to `StripeGateway`. The live version uses `client.v1.invoices.retrieve`; check the SDK. The fake supports setting a status per id. Live-gateway request test with the stubbed HTTP client, as before.

## The daily run (`src/collection_agent/daily.py`)

`run_daily(session_factory, gateway, identity, today, *, dry_run=False) -> DailyReport`. Pure orchestration, every dependency injected:

1. `policy = get_approved_policy(...)`. If none, raise. The CLI exits non-zero; never fall back to defaults.
2. `cal = uk_calendar([today.year - 1, today.year, today.year + 1])`. If `today` isn't a working day, return an empty report straight away: no Stripe calls, no writes.
3. For each id in `list_open_invoice_ids`, in **its own transaction** (one invoice failing never stops the others):
   1. Skip if the invoice already has a reminder-kind outbox row in `pending_approval` or `sending`. Outcome: `awaiting_approval`.
   2. `inputs = load_decision_inputs(...)`; `action = decide_next_action(inputs..., policy, today, cal)`. If `None`, outcome `nothing_due`.
   3. **Re-check Stripe** (`get_invoice_status`) only when an action is due:
      - `paid` → set `status=paid`, outcome `paid_in_stripe`.
      - `void` → `status=void`.
      - `uncollectible` → `state=with_human`.
      - `open` → continue.
      - Anything else, or a Stripe error → no change, outcome `stripe_error`, and log the Stripe request id only.
   4. `ESCALATE_TO_HUMAN` → set `invoices.state=escalated` and a new `escalated_at` (timestamptz, migration `0004`). Outcome `escalated`.
   5. Reminder actions → render the template and insert a `pending_approval` outbox row. From/Reply-To come from the identity, exactly as the invoice email does (reuse that code). Outcome `drafted:<kind>`. A unique-index race counts as `awaiting_approval`.
4. `DailyReport` holds `run_date` and a per-invoice list of `(invoice_id, reference, outcome)` plus counts by outcome.
5. With `dry_run=True`: still decide and render, but no Stripe calls and no writes, and the session is rolled back. Outcomes are prefixed `would_`.

Also store a summary row per run in a new `daily_runs` table: `id`, `run_date`, `started_at`, `finished_at`, `dry_run`, `counts` (JSONB). Re-running on the same day is safe because of the outbox guard; it just records another run.

## CLI (`src/collection_agent/cli.py`, `argparse`, no new dependency)

Entry point `collection-agent` in `[project.scripts]`, also runnable as `uv run python -m collection_agent.cli`:
- `run-daily [--date YYYY-MM-DD] [--dry-run]`
  - Default date is today in Europe/London. Prints the report as JSON.
  - Exit code 0 on success, 1 for no approved policy or missing required settings, 2 if any invoice ended in `stripe_error` or an unexpected error.
  - Requires `DATABASE_URL`, plus `STRIPE_SECRET_KEY` and the email identity settings unless `--dry-run`. Fails closed with a clear message, never printing secret values.
- `policy create-default`: inserts a draft with the decided defaults and prints its version.
- `policy show`: prints the approved policy, or a clear message if none.
- `policy approve --version N --by NAME`: uses `approve_policy`.

## Approving reminders (changes to slice-4 approve)

In `POST /outbox/{id}/approve`, after a successful send of a reminder kind, in the same transaction that marks the row `sent`, call `record_reminder(invoice_id, <ReminderKind>, sent_on=today_london)`. The invoice-kind behaviour is unchanged. Existing approve safety (row lock, invoice-still-open check, `sending` claim, outcome-unknown handling) applies to reminders unchanged.

## Resolving stuck sends (new)

`POST /outbox/{id}/resolve` (admin):
- Body: `{"resolved_by": "...", "outcome": "sent" | "failed", "provider_message_id": "..."}`. `provider_message_id` is optional, for `sent` only.
- Only from status `sending`, else 409.
- `sent` applies exactly the step-3 effects: `sent_at`, `invoices.sent_on` for the invoice kind if null, `record_reminder` for reminder kinds. `failed` sets `failed`.
- Store who resolved it: `resolved_by` and `resolved_at` columns (migration `0004`).

## Escalations list (new)

`GET /invoices?state=escalated` (admin) lists escalated invoices, newest `escalated_at` first: id, reference, customer name and email, amount, `due_on`, `escalated_at`, reminder count. Support only the `state` filter for now.

## Tests (DB ones marked `db`)

Use the injected clock and the fakes. Required:
- Templates: as above.
- `run_daily`:
  - Weekend → empty, with zero Stripe calls.
  - No approved policy → raises.
  - Unsent invoice not touched.
  - Due `REMIND` → one `pending_approval` row with the exact subject/body.
  - Second run the same day → `awaiting_approval`, still one row.
  - After that reminder is approved (sent), next week's run drafts level 2.
  - Squeeze scenario from slice 1 → `reminder_pre_deadline` drafted.
  - Broken promise → `reminder_broken_promise` with the promised date in the text.
  - Max reminders reached + silence → `escalated`, `state`/`escalated_at` set, no draft.
  - Stripe says paid → status updated, no draft.
  - Stripe error on one invoice → that one `stripe_error`, others still processed.
  - Dry run → no rows written (including `daily_runs`), no Stripe calls, `would_` outcomes.
  - A `daily_runs` row is written on a normal run.
- Approve a reminder → `reminders` row with the right kind and London date; then `decide_next_action` sees it.
- Resolve: `sent` on a stuck invoice email sets `sent_on`; `sent` on a stuck reminder records the reminder; `failed`; not-`sending` → 409; auth.
- Escalations list: filter and ordering; auth.
- CLI:
  - `policy create-default` → `approve` → `show` round-trip.
  - `run-daily --dry-run --date ...` prints JSON and exits 0.
  - No approved policy → exit 1.
  - Missing Stripe key without `--dry-run` → exit 1 with a message that names the variable but no value.
  - Call `main(argv)` directly; don't spawn processes.
- Migration `0004` round-trips; the new partial unique index blocks a second active reminder for one invoice.
- `LiveStripeGateway.get_invoice_status` request shape (no network).
- All earlier tests still pass.

## Done when

- `docker compose up -d db`, then all pass:
  - `uv run pytest`
  - `uv run pytest -m "not db"`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run alembic upgrade head` on the dev DB
  - `uv run collection-agent run-daily --dry-run` against the dev DB (it may exit 1 for "no approved policy"; report what it printed)
- `CLAUDE.md` `## Commands` gains the CLI commands.
- No network calls to Stripe or Postmark in tests. No real keys. `.env` not created or edited. Nothing committed.
