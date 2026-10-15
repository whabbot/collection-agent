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
| `CLAUDE.md` (repo root) | Read by Claude Code every session; imports decisions + architecture, sets the working rules, lists the commands |
| `docs/handoff.md` | This file: status and next steps |
| `docs/decisions.md` | What's decided, what's still a proposed default, open questions |
| `docs/architecture.md` | The design: Stripe invoices, hard vs soft policy split, components, state machine, reply handling, guardrails |
| `docs/jev.md` | Jev (TypeSafe) reply-classifier notes: SDK, known weaknesses, what they change in the design |
| `docs/setup-and-inputs.md` | Accounts, credentials, env var names, inputs still needed, how to re-create the Cognee setup |
| `docs/plans/slice-N-*.md` | The spec each slice was built from (1–9) |
| `docs/demo-runbook.md` | Demo setup, date picking, click-path, reset (written by the slice-9 builder) |

## Where things stand (15 Oct 2026, end of session)

**`main` is pushed with slices 1–9.** Migrations are at `0008`.

**Slice 9 (demo page) is committed.**
- 856 tests pass. Tests now never read `.env` (session guard plus a test that proves it).
- Checked in the browser with no keys: the page loads, the token dialog works, the three-column layout is right at 1600 px, and errors show as visible toasts.
- The full click-through with real services hasn't been done yet.
- Builder deviations kept:
  1. `GET /invoices` with no filter lists recent invoices.
  2. Python-side `created_at` defaults, so events within one transaction keep their order.
  3. The timeline orders by action time.
  4. `demo reset` clears all demo tables but keeps the clock. It's guarded by `DEMO_MODE` and the DB name.
- Cosmetic: "View Cognee graph" still shows when Cognee isn't configured.

**Incident, 15 Oct ~20:52 UK:** while checking the page, the local server read the project `.env`. Launch-config unsets don't stop pydantic-settings reading that file. As a result, the sample policy (`tests/fixtures/policy_sample.txt`) was sent to the real Cognee tenant: it created the `policies` dataset (1 item) and started cognify. No Claude or Stripe calls were made. The user chose to **keep** it (15 Oct). To run the app with no keys, start it from a directory with no `.env` (see `.claude/launch.json`, not committed).

### Built so far

| Slice | What it does |
|---|---|
| 1 | `ChasePolicy`, the England & Wales `WorkingCalendar`, and the pure `decide_next_action` (unchanged since; uses the stored `due_on`) |
| 2 | FastAPI app, Postgres 18 (`compose.yaml`, localhost:55432), SQLAlchemy 2, Alembic, repository layer |
| 3 | `POST /invoices`: Stripe customer + finalized invoice, `auto_advance=false`, idempotent on `reference`. `POST /webhooks/stripe`: signature check, dedupe; paid / void / uncollectible |
| 4 | Contract PDF upload, plain-text invoice email, approval outbox (`/outbox`, approve / reject / resolve), Postmark sender (tracking off, no auto-retry). Sending the invoice email sets `sent_on` |
| 5 | `collection-agent run-daily [--date] [--dry-run]`: re-checks Stripe, then drafts a template reminder (friendly → firm → formal) or escalates. `policy create-default / approve / show` |
| 6 | `POST /webhooks/inbound` (Postmark): dedupe, auto-reply drop, matching. Jev classifier (`jev-1.13.0`). Code routing table → template reply drafts. `/inbox` review log |
| 7 | Rule tags (`describe_rule`) on reminder drafts, sent reminders and escalations, e.g. "No response for 7 days (last contact …)" |
| 8 | `POST /policies/documents`: Cognee `policies` ingest + Claude (`claude-opus-5-5`) extraction into a `ChasePolicy` draft. A value is used only if its quote is verbatim, states the number and isn't an injected instruction. Rule cards, approve, Cognee graph proxy, CLI `policy ingest` |
| 9 | `/demo` page, demo clock + "Skip 7 days", simulate customer reply, `EMAIL_SENDER=log`, `GET /invoices/{id}/timeline`, `demo reset` |

Guardrails that still hold:
- Hard chase timing comes only from the approved `ChasePolicy`. Nothing at chase time calls Claude or Cognee.
- Customer email is untrusted. No code path changes amounts, currency or due dates.
- Paid is set only after Stripe confirms it.
- Nothing is sent without human approval.

## Current priority: the hackathon demo

A 60-second video (storyboard sent by the "Hackathon demo storyboard" session; recorded in `decisions.md`):
1. 0–5s: an awkward "following up" email.
2. 5–15s: paste the policy → Cognee ingests it, Claude extracts rule cards → approve.
3. 15–25s: invoice with Stripe link + contract → "Skip 7 days" → a reminder tagged with its rule.
4. 25–40s: customer replies "we'll pay on the 12th" → Jev says promise to pay → acknowledgement drafted, approved, sent → chasing pauses.
5. 40–50s: paid in Stripe → the timeline closes.
6. 50–60s: end card (Stripe / Cognee / Claude logos, repo URL).

Date gotcha: if the skip lands in the 3-working-day pre-deadline window you get `pre_deadline` instead of the 7-day `no_response`. Start the clock early in the month on a weekday (e.g. Thu 1 Oct 2026 → due Mon 16 Nov 2026 → skip to Thu 8 Oct). Check with `run-daily --dry-run --date`.

## Next steps (in order)

1. **The user adds keys to `.env`** (Claude cannot create accounts or enter keys):
   - `STRIPE_SECRET_KEY`: a Stripe **sandbox** restricted key (`rk_…`).
   - `STRIPE_WEBHOOK_SECRET`: from `stripe listen --forward-to localhost:8000/webhooks/stripe`.
   - `ANTHROPIC_API_KEY`: policy extraction.
   - `TYPESAFE_API_KEY`: Jev reply classification.
   - `COGNEE_BASE_URL`, `COGNEE_API_KEY`: from `~/.cognee/.env`; **rotate the key first** (it was pasted into chat on 15 Oct).
   - `ADMIN_API_TOKEN`: any long random string.
   - `DEMO_MODE=true`, and `EMAIL_SENDER=log` (safest for the video) or Postmark (`POSTMARK_SERVER_TOKEN` + confirmed sender + `EMAIL_FROM_*` / `REPLY_TO_DOMAIN` / `COMPANY_NAME`). A new Postmark account may only send to its own domain until approved, so check that.
   - The Stripe webhook endpoint must use the API version the installed `stripe` SDK pins (`2026-09-30.endive`).
2. **Run the two opt-in live checks** once the keys are in (they cost money; confirm first):
   - `uv run pytest -m live`: Claude extraction on `tests/fixtures/policy_sample.txt` (expects max reminders 3, not the injected 50) and the Jev classifier.
   - `uv run collection-agent eval-classifier`: Jev accuracy, date accuracy, option-order flips.
3. **Full demo rehearsal** with the real services: approve the policy, then `demo reset` between takes. Replace the hand-written Stripe fixtures in `tests/fixtures/stripe/` with real `stripe listen` captures.
4. After the demo:
   - choose hosting for the daily job (proposed: weekdays 08:00 UK);
   - Claude drafting of reminders once tone examples exist;
   - Cognee soft-guidance recall for drafting;
   - a reviewer UI beyond the demo page.

## Working method

- The main session orchestrates. It writes a spec in `docs/plans/`, hands the build to a coding subagent (**Sonnet, medium effort**, per the user), then re-runs the checks and reads the risky code itself before committing. The user doesn't want the orchestrator writing code itself.
- Builders work on `main` and leave changes uncommitted. The orchestrator commits straight to `main` and pushes. **No feature branches or PRs** (the user's instruction).
- Before building on anything still marked "proposed" in `decisions.md`, ask the user. Record their answer as "Decided".
- The repo is **public**: never commit real keys. The compose DB password is a throwaway, and the DB port is bound to 127.0.0.1.

## Decisions made this session (details in `decisions.md`)

| Topic | Decision |
|---|---|
| Stack | Python 3.14 + FastAPI, Postgres 18, SQLAlchemy 2, Pydantic 2, managed with `uv` |
| Clearance deadline | 15th of the month after sending |
| `ChasePolicy` defaults | 7 / 3 / 3 / 2 |
| Email | Postmark |
| Reply classifier | Jev (`TYPESAFE_API_KEY`), no summary field |
| Reminder drafting (v1) | Templates |
| Reply routing table | Confirmed |
| Promise-to-pay limit | Up to `due_on` |
| Claude | `claude-opus-5-5`, policy extraction only |
| Priority | The hackathon demo comes first |

## Open points

- **Deadline month when sending is delayed:** `due_on` is fixed at invoice creation. If approval delays the first email past a month end, should it follow the sent month instead?
- **Contract-specific due dates:** they could override `due_on` at creation.
- **Duplicate customers:** two concurrent first invoices for one new email can create two Stripe customers. There's no unique constraint on email, because AP inboxes can be shared.
- **Multi-currency:** a Stripe customer is single-currency, so invoicing in a second currency returns 502.
- **Escalations:** nobody is notified yet (open: who receives them).
- **Approval queue location:** the outbox is API-only apart from the demo page. Where it should live (email, Slack, admin page) is open.
- **Sender identity** (person vs team): `EMAIL_FROM_NAME`, no code change needed.
- **Rule-tag nit:** `broken_promise_at_max` is unreachable. It should key on "promise broken at max". The label is the same either way.
- **Starlette warning:** `TestClient` via `httpx` is deprecated (suggests `httpx2`).
- **Still proposed, not decided:** hosting, scheduler time, "taste" defaults, sender identity, escalation contacts, phrases never to use, whether this is UK B2B (late-payment interest needs legal advice).
- **Inputs still wanted:** real policy docs, a sample contract, 2–3 good and 1 bad chase email, 10–20 real customer replies.
