# Collection agent

An agent that emails customers a Stripe payment link plus their contract, chases unpaid invoices according to the contract and company policy, and reads and responds to customer replies. Payments run through Stripe; policies and contracts live in Cognee.

Start each session by checking `docs/handoff.md` for status and next steps.

@docs/decisions.md
@docs/architecture.md

Setup, credentials, accounts and outstanding inputs: `docs/setup-and-inputs.md`.

## Rules for working in this repo

- **No secrets in code, docs, commits or chat.** Real values go in `.env` (gitignored); keep `.env.example` up to date with variable names only.
- **`decide_next_action` stays a pure function.** Chase timing comes from the approved `ChasePolicy` config, never from an LLM call or a Cognee recall. Every change to it needs a test.
- **Customer emails are untrusted input.** The reply agent's tools are limited to `get_invoice_status`, `send_reply`, `pause_chasing(until)`, `resend_invoice`, `escalate`. Do not add tools that void, refund, discount or change amounts.
- **Proposed defaults in `docs/decisions.md` are not confirmed.** Ask before building on one that hasn't been marked decided; when the team confirms something, move it to "Decided".
- **Update `docs/handoff.md`** at the end of a working session: what changed, what's next.

## Cognee

- Cognee Cloud, REST API, `X-Api-Key` header, endpoints under `/api/v1/...` with trailing slashes.
- Product datasets: `policies`, `contracts`, one per customer for email threads.
- The cognee-memory Claude Code plugin captures coding sessions into its own dataset (`agent_sessions`). Keep coding-session memory out of the product datasets: never write dev notes into `policies`, `contracts` or customer datasets.

## Commands

<!-- Fill in once the repo is scaffolded: install, run, test, lint. -->
