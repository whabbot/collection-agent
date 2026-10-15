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

## Design decisions (proposed in the design, adopted as the working approach)

- Use **Stripe Invoices**, not Payment Links. The invoice's `hosted_invoice_url` is the payment link in the email. Invoices carry customer, amount and due date, and emit webhooks (`invoice.paid`, `invoice.payment_failed`, and `invoice.overdue` / `invoice.will_be_due` via Billing Automations).
- **Turn off Stripe's built-in reminder emails** so customers aren't chased twice.
- **Hard rules are typed config, not Cognee retrieval.** Timing rules (7 days, 15th, max reminders) are extracted from policy docs into a Pydantic `ChasePolicy`, approved by a human, and read by the scheduler. Cognee holds soft guidance: tone, wording, exceptions, contract text, per-customer history.
- **The LLM never decides when to chase or what it's allowed to do.** `decide_next_action` is a pure, unit-tested function. The LLM drafts emails and classifies replies.
- **Reply handling = classify → route by a code-defined table.** Customer emails are untrusted input; the agent has no tools that change money.
- **Approval queue first.** All drafts (outbound and replies) go to a human for the first few weeks; auto-send is enabled per intent once accuracy is proven.
- Contract payment terms are stored as structured data (due date + invoice metadata) at invoice creation, never re-derived from the contract.

## Proposed defaults — awaiting confirmation

### Stack (BLOCKING before scaffolding)

| Item | Proposed |
|---|---|
| Language / framework | Python 3.12 + FastAPI, Postgres, SQLAlchemy |
| Repo | New GitHub repo — **name needed** (`owner/repo`) |
| Email provider (send + inbound) | Postmark |
| LLM | Claude via Anthropic API |
| Hosting | Render / Railway / Fly (needs a persistent worker + daily cron) |
| Scheduler | Daily job, 08:00 UK time, weekdays only |

### Policy numbers

| Rule | Proposed |
|---|---|
| Days of no response before chasing | 7 |
| Clearance deadline | 15th of the month — **which month? (sent month or following)** |
| Pre-deadline reminder buffer | 3 days before the deadline |
| Max reminders before human escalation | 3 |
| Latest acceptable promise-to-pay date | No later than the clearance deadline |
| Grace after a missed promise | 2 business days |
| Agent can agree extensions / payment plans | No — always human |
| Weekends & bank holidays | No sends; deadlines falling on them roll to next working day |

### Reply handling routing

| Intent | Action | Auto-reply? |
|---|---|---|
| Already paid | Verify in Stripe; if paid thank + stop, else ask for remittance reference | Yes |
| Promise to pay | Within policy → pause until date + grace and confirm; else human | Yes, within policy |
| Extension / payment plan | Human | Holding reply only |
| Dispute | Stop chasing immediately, human | Holding reply only |
| Admin request | Resend invoice/contract; billing-detail changes → human | Partly |
| Contract question | Answer from contract via Cognee only if a clause can be quoted; else human | Yes, if grounded |
| Out of office | Not a response; schedule unchanged | No |
| Other / low confidence | Human | No |

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
