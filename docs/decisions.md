# Decisions

Three kinds of entry: **Decided** (the team said so), **Proposed default** (suggested, not yet confirmed — confirm or change), and **Open question** (no answer yet).

## Decided

| Topic | Decision |
|---|---|
| Payments | Stripe |
| Policy store | Cognee — specifically **Cognee Cloud** (tenant already created) |
| Outbound | Agent emails the customer a Stripe payment link and the contract |
| Chasing | Agent chases unpaid invoices according to the contract and company policy |
| Inbound | Agent listens to customer replies to chase emails and responds |
| Policy examples given | Follow up after **1 week of no response**; payments should **clear by the 15th of the month** |
| Stack | Python 3.14 (latest stable) + FastAPI, Postgres, SQLAlchemy 2.x, Pydantic 2.x, managed with `uv`. Latest compatible versions of everything (decided 15 Oct 2026) |
| Repo | `whabbot/collection-agent` |
| Clearance deadline | 15th of the month **following** the month the invoice was sent; rolls to the next working day if it falls on a weekend or bank holiday |
| Email provider | **Postmark** for sending and inbound (decided 15 Oct 2026) |
| Reminder drafting (v1) | **Fixed plain-text templates** (friendly → firm → formal by reminder number), no LLM. Claude drafting comes later, once tone examples and policy docs exist. Every draft is human-approved (decided 15 Oct 2026) |
| Reply routing (v1) | Code table, decided 15 Oct 2026. Every reply we draft goes through the approval outbox as template text (no LLM). **Already paid** → verify in Stripe; if paid, mark paid + thank-you draft; else draft asking for a remittance reference. **Promise to pay** → if the date is on or before the invoice's `due_on`: set `promised_date`, pause until promise + grace, draft acknowledgement; otherwise human. **Extension / payment plan** → human + holding-reply draft. **Dispute** → stop chasing (`disputed`) + human + holding-reply draft. **Admin: resend** → draft resend with link + contract; **admin: billing-detail change** → human + holding reply. **Contract question** → human (until contracts are in Cognee). **Out of office** → not a response, ignored. **Other / low confidence** → human, no draft |
| Promise-to-pay limit | The agent may accept a promised date no later than the invoice's `due_on`; anything later goes to a human (decided 15 Oct 2026) |
| LLM | **Claude via Anthropic API**, used first for one thing: reading pasted policy text and proposing a `ChasePolicy` draft, which a human approves. Reminder and reply drafts stay as reviewed templates for now (decided 15 Oct 2026) |
| Hackathon demo | A 60-second demo is the current priority. Order: rule tags on reminders → Cognee policy ingest + Claude extraction into rule cards + approve → one-page demo UI (decided 15 Oct 2026) |
| Reply classifier | **Jev** (TypeSafe): typed decisions with calibrated probabilities. Use it directly; no comparison against other models. Classification only; drafting still needs a generative LLM. Key in `TYPESAFE_API_KEY` (the SDK default). No `summary` field in v1: Jev can't generate text, and reviewers read the email itself. See `docs/jev.md` (decided 15 Oct 2026) |
| `ChasePolicy` defaults | 7 days no response; pre-deadline reminder 3 working days before the deadline; escalate after 3 reminders; 2 business days grace after a missed promise; no sends on weekends or England & Wales bank holidays (decided 15 Oct 2026) |

## Design decisions (proposed in the design, adopted as the working approach)

- Use **Stripe Invoices**, not Payment Links. The invoice's `hosted_invoice_url` is the payment link in the email. Invoices carry customer, amount and due date, and emit webhooks (`invoice.paid`, `invoice.payment_failed`, and `invoice.overdue` / `invoice.will_be_due` via Billing Automations).
- **Turn off Stripe's built-in reminder emails** so customers aren't chased twice.
- **Hard rules are typed config, not Cognee retrieval.** Timing rules (7 days, 15th, max reminders) are extracted from policy docs into a Pydantic `ChasePolicy`, approved by a human, and read by the scheduler. Cognee holds soft guidance: tone, wording, exceptions, contract text, per-customer history.
- **The LLM never decides when to chase or what it's allowed to do.** `decide_next_action` is a pure, unit-tested function. The LLM drafts emails; Jev classifies replies.
- **Reply handling = classify → route by a code-defined table.** Customer emails are untrusted input; the agent has no tools that change money.
- **Approval queue first.** All drafts (outbound and replies) go to a human for the first few weeks; auto-send is enabled per intent once accuracy is proven.
- Contract payment terms are stored as structured data (due date + invoice metadata) at invoice creation, never re-derived from the contract.

## Proposed defaults — awaiting confirmation

### Stack (remaining items)

| Item | Proposed |
|---|---|
| Hosting | Render / Railway / Fly (needs a persistent worker + daily cron) |
| Scheduler | Daily job, 08:00 UK time, weekdays only |

### Policy numbers

Timing numbers are decided (see above). Still proposed:

| Rule | Proposed |
|---|---|
| Agent can agree extensions / payment plans | No — always human |

### Taste (proposed)

- Plain-text emails rather than branded HTML.
- Tone escalates: friendly → firm → formal.
- No legal threats or "final notice" wording before reminder 3.

## Open questions

- Sender identity: a named person ("Sarah from Accounts") or a team ("YourCo Billing")?
- Must the customer sign the contract before paying, or is it sent for information? (Proposal: no e-signature in v1.)
- One-off invoices only, or recurring too?
- Roughly how many invoices per month?
- Who reviews the approval queue, and where (email, Slack, admin page)?
- Who receives escalations: disputes vs extension requests?
- Phrases the agent must never use.
- Is this B2B in the UK? If so, decide whether statutory late-payment interest belongs in the escalation policy (needs proper legal advice).
