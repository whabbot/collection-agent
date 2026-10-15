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
- [x] Stack, clearance month and `ChasePolicy` numbers confirmed (see `decisions.md` → Decided)
- [x] GitHub repo: `whabbot/collection-agent`
- [x] Cognee key in `~/.cognee/.env` verified working (200 on `/api/v1/datasets/`, 15 Oct)
- [x] **Slice 1 built** on branch `slice-1-chase-policy` (spec: `docs/plans/slice-1-chase-policy.md`): uv + Python 3.14 skeleton, `WorkingCalendar`/`uk_calendar`, `ChasePolicy`, `decide_next_action` + 56 tests; pytest, ruff, ruff format, mypy strict all pass
- [ ] Confirm remaining proposed defaults (email provider, hosting, scheduler, promise-date limit, reply routing, taste)
- [ ] Collect inputs: policy docs, sample contract, example chase emails, sample customer replies
- [ ] Stripe test-mode keys + webhook secret
- [ ] Email provider account + domain DNS
- [ ] Rotate the Cognee API key (it was pasted into chat on 15 Oct) — current key works; rotation not confirmed

## Working method

The main session orchestrates: it writes a spec in `docs/plans/`, then hands the build to a coding subagent (**Sonnet, medium effort**), then re-runs the checks itself before reporting.

## Next steps

1. Review and commit slice 1 (`slice-1-chase-policy`), open a PR into `main`.
2. Slice 2: FastAPI + SQLAlchemy/Postgres skeleton, persisting invoices/reminders/replies and the approved `ChasePolicy`. When the scheduler is built, build the calendar with `uk_calendar([today.year, today.year + 1])` — deadlines cross into January.
3. Then: Stripe webhook handler (recorded fixtures), reply classifier with synthetic test cases, approval queue.
4. When policy docs arrive: load them into the Cognee `policies` dataset and test recall before deciding how much to extract into typed config.
5. Open point from slice 1: should a contract-specific due date override the policy deadline? Currently the policy deadline is the only one.
