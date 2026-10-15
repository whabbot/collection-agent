# Architecture

## Overview

```
FastAPI service
├── POST /invoices          → create Stripe customer + invoice, store contract, send first email
├── POST /webhooks/stripe   → invoice.paid / payment_failed / overdue → update state
├── POST /webhooks/inbound  → customer reply → classify → route (see Reply handling)
├── Daily job (cron/APScheduler)
│     for each open invoice:
│        action = decide_next_action(invoice, policy, history)   ← deterministic
│        if action: draft = agent.draft(action, cognee context)   ← LLM
│                   send, or queue for approval
└── Postgres: customers, invoices, chase_events, inbound_messages, policies
```

## Stripe

- Stripe **Invoices** with a `due_date`; the email links to `hosted_invoice_url`.
- Contract terms recorded at creation: `due_date`, plus `contract_id` and terms in invoice metadata.
- Webhooks: `invoice.paid`, `invoice.payment_failed`; Billing Automations can emit `invoice.overdue` (X days after due) and `invoice.will_be_due` (X days before).
- Stripe's own reminder emails switched **off** — the agent owns chasing.
- Verify webhook signatures with the signing secret.

## Policies: hard rules vs soft guidance

| Kind | Examples | Where |
|---|---|---|
| Hard rules | 7 days no response, clear by 15th, max 3 reminders, escalate after 3rd | Typed `ChasePolicy` (Pydantic, stored in Postgres), extracted from policy docs, human-approved |
| Soft guidance | Tone, escalation wording, key-account exceptions, dispute handling, relationship context | Cognee |

Flow: policy docs → Cognee → one-off extraction proposes `ChasePolicy` → human approves → scheduler reads only the approved config.

Reason: Cognee is semantic memory (LLM-built knowledge graph); recall is not guaranteed to return a rule identically every time, and "is today the day to chase?" must be deterministic.

## Cognee

- Using **Cognee Cloud** via REST: base URL + `X-Api-Key` header, endpoints under `/api/v1/...` (e.g. `GET /api/v1/datasets/` works; note the trailing slash — without it the API returns a 307 redirect).
- Cognee v1.0 Python API uses `remember()` / `recall()`; legacy `add()` / `cognify()` / `search()` still available. Most tutorials use the legacy calls.
- Datasets:
  - `policies` — company and chasing policy docs
  - `contracts` — customer contracts (for grounded contract Q&A)
  - one dataset per customer — email thread history (e.g. "promised twice, missed both")

## Core decision function

Pure function, fully unit-tested without LLM or Cognee:

```python
def decide_next_action(inv, policy, events, today) -> Action | None:
    if inv.status in ("paid", "void") or inv.state in ("disputed", "with_human"):
        return None
    if inv.paused_until and today < inv.paused_until:
        return None
    if inv.promised_date and today > inv.promised_date + timedelta(days=policy.promise_grace_days):
        return Action.REMIND_BROKEN_PROMISE
    last = events[-1].sent_at if events else inv.sent_at
    chase_due = last + timedelta(days=policy.no_response_days)
    clear_by = policy.clearance_deadline(inv)  # e.g. 15th of the month
    if len(events) >= policy.max_reminders:
        return Action.ESCALATE_TO_HUMAN
    if today >= chase_due or today >= clear_by - timedelta(days=policy.pre_deadline_buffer):
        return Action.REMIND
    return None
```

Must-have test: invoice sent on the 12th — the 7-day rule says chase on the 19th but the money had to clear by the 15th. The pre-deadline buffer resolves the conflict.

## State machine

```
sent ──(no reply, policy timer)──▶ reminded ──▶ … ──▶ escalated
  │                                  │
  └── reply ──▶ classify ──┬── paid ──────────────▶ closed
                           ├── promise ──▶ paused ──(missed)──▶ reminded
                           ├── dispute ──▶ with_human
                           └── question/admin ──▶ answered ──▶ back to timer
```

## Reply handling

### Ingestion
- Inbound email provider POSTs replies to `/webhooks/inbound`.
- Send each email from a per-invoice reply address, e.g. `reply+inv_123@billing.<domain>`, so a reply maps to its invoice even if the subject is edited or the email forwarded.
- Fallback matching: `In-Reply-To` / `References` headers, then sender address vs Stripe customers. No match → human.
- Dedupe on `Message-ID` (inbound webhooks can arrive more than once).
- Drop `Auto-Submitted: auto-replied` / out-of-office before classifying; cap the agent at ~2 replies per thread per day to avoid bot-to-bot loops.

### Classification (Jev, typed decision with calibrated probability)

Jev's probability fills `confidence`; per-intent thresholds on it decide human review vs auto-send.

```python
class Intent(str, Enum):
    ALREADY_PAID = "already_paid"
    PROMISE_TO_PAY = "promise_to_pay"
    REQUEST_EXTENSION = "request_extension"
    REQUEST_PAYMENT_PLAN = "request_payment_plan"
    DISPUTE = "dispute"
    ADMIN_REQUEST = "admin_request"
    CONTRACT_QUESTION = "contract_question"
    OUT_OF_OFFICE = "out_of_office"
    OTHER = "other"


class ReplyClassification(BaseModel):
    intent: Intent
    confidence: float
    # Assembled in code from Jev date-part Choices; see docs/jev.md.
    promised_date: date | None = None
```

Routing table is in code (see `decisions.md`), not in the prompt.

## Guardrails

- Customer emails are **untrusted input**. Assume someone will write "your system already agreed to waive this".
- Agent tools are limited to: `get_invoice_status`, `send_reply`, `pause_chasing(until)`, `resend_invoice`, `escalate`. No void, refund, discount or amount changes.
- `pause_chasing` validates the date against `ChasePolicy` before applying.
- The agent only states facts it can confirm in Stripe or the contract; never repeats commitments a customer claims were made.
- Contract answers must quote the clause relied on; otherwise escalate.

## Email

- Transactional provider (proposed: Postmark) on a verified subdomain with SPF, DKIM, DMARC, and an MX record for inbound.
- Contract PDF attached by us, alongside the hosted invoice link.

## Rollout

1. All drafts (outbound + replies) go to an approval queue.
2. Log each classification next to what the reviewer actually did.
3. Enable auto-send per intent once accuracy is proven. Likely first: already-paid confirmations, invoice resends, promise-to-pay acknowledgements. Disputes stay with a human permanently.
