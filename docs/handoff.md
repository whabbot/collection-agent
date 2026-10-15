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
| `docs/setup-and-inputs.md` | Accounts, credentials, inputs still needed, and how to re-create the Cognee setup in a new session |

## Status (15 Oct 2026)

- [x] Architecture designed (outbound chasing + inbound reply handling)
- [x] Cognee Cloud tenant created; API key verified working (tenant has one empty dataset, `default_dataset`)
- [x] `cognee-memory` Claude Code plugin installed (in the previous session's workspace only)
- [ ] Confirm proposed defaults in `decisions.md` (stack, policy numbers, scope)
- [ ] Create GitHub repo and attach it to the session
- [ ] Collect inputs: policy docs, sample contract, example chase emails, sample customer replies
- [ ] Stripe test-mode keys + webhook secret
- [ ] Email provider account + domain DNS
- [ ] Rotate the Cognee API key (it was pasted into chat on 15 Oct)

## Next steps for the new session

1. Read `docs/decisions.md` and get the **blocking** items confirmed: stack and repo name.
2. Re-run the Cognee setup (see `docs/setup-and-inputs.md`). Credentials from the previous session's workspace do not carry over.
3. Scaffold the repo with: `ChasePolicy` model, `decide_next_action` + tests (including the 12th-vs-15th edge case), Stripe webhook handler, reply classifier with synthetic test cases, approval queue.
4. When policy docs arrive: load them into the Cognee `policies` dataset and test recall before deciding how much to extract into typed config.
