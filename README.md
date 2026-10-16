# Collection agent

An agent that collects unpaid invoices:
1. It emails the customer a Stripe payment link and their contract.
2. It chases them according to the company's own chasing policy.
3. It reads their replies and either answers or hands off to a person.

Built for a hackathon in October 2026. Repository: <https://github.com/whabbot/collection-agent>

## How it works

```
policy text ──▶ Cognee (soft guidance, graph) ─┐
            └─▶ Claude proposes ChasePolicy ───┴─▶ rule cards ─▶ human approves
                                                                     │
POST /invoices ─▶ Stripe invoice (hosted pay link) ─▶ invoice email ─┤ approval outbox
                                                                     │ (nothing is sent
daily run ─▶ decide_next_action (pure, policy-driven) ─▶ reminder ───┤  without a person)
                                                                     │
customer reply ─▶ /webhooks/inbound ─▶ classifier (Jev or Claude) ─▶ code routing table
Stripe invoice.paid ─▶ /webhooks/stripe ─▶ invoice closed
```

- **Hard rules are typed config.** The 7-day follow-up, "clear by the 15th of the following month", the pre-deadline reminder, max reminders and promise grace all live in an approved `ChasePolicy`. The scheduler reads only that. Chase timing never comes from an LLM or a Cognee recall.
- **Claude** reads pasted policy text and proposes the `ChasePolicy`. A value is used only if its quote appears verbatim in the text, states that number, and isn't an injected instruction. A person approves the result as rule cards.
- **Cognee** stores the policy text (`policies` dataset) for soft guidance and the knowledge graph.
- **Stripe** invoices are created with `auto_advance=false`, so Stripe never emails or chases the customer; the agent does. Webhooks mark invoices paid, void or handed to a person.
- **Reminders** are plain-text templates, friendly → firm → formal. Each is tagged with the rule that triggered it, e.g. "No response for 7 days (last contact …)".
- **Replies** are classified by Jev (TypeSafe) or Claude (`CLASSIFIER=jev|claude`), then routed by a code table:
  - promise to pay → pause chasing and acknowledge;
  - dispute → stop chasing and send to a person;
  - already paid → check with Stripe first, never trust the email.

  Customer emails are treated as untrusted input. Nothing they say can change amounts or due dates.
- **Every outbound email** goes through an approval outbox before it's sent.

## Stack

Python 3.14, FastAPI, Postgres 18, SQLAlchemy 2, Alembic, Pydantic 2, managed with `uv`. Stripe, Postmark, the Anthropic API (`claude-sonnet-5-5`, low effort), TypeSafe Jev and Cognee Cloud.

## Quick start

```bash
uv sync
```

```bash
docker compose up -d db
```

```bash
cp .env.example .env
```

Fill in `.env`; every variable is described in `docs/setup-and-inputs.md`. At minimum you need `DATABASE_URL` and `ADMIN_API_TOKEN`. Then:

```bash
uv run alembic upgrade head
```

```bash
uv run uvicorn collection_agent.app:app --port 8000
```

Check it's up at `GET /healthz`. Nothing chases until a policy is approved:

```bash
uv run collection-agent policy create-default
```

```bash
uv run collection-agent policy approve --version 1 --by "Your Name"
```

The daily job is `uv run collection-agent run-daily` (add `--dry-run` to preview).

### Demo

With `DEMO_MODE=true` and `EMAIL_SENDER=log` in `.env`, open `http://localhost:8000/demo`. Paste your `ADMIN_API_TOKEN` when asked, then follow `docs/demo-runbook.md`:

policy → rule cards → invoice → **Skip 7 days** → tagged reminder → customer reply "we'll pay on the 12th" → promise accepted → pay with Stripe test card 4242 → **Paid**.

## Tests

```bash
uv run pytest
```

```bash
uv run ruff check .
```

```bash
uv run mypy src
```

- 888 tests. DB tests need the compose Postgres; `-m "not db"` runs the rest.
- Tests never read `.env` and never call external services.
- Opt-in live tests read `LIVE_*` variables; see `CLAUDE.md`.

## Docs

| File | What's in it |
|---|---|
| `docs/next-steps.md` | What to do next, in priority order |
| `docs/handoff.md` | Status, rehearsal results, open points |
| `docs/decisions.md` | Every decision made, plus what's still proposed or open |
| `docs/architecture.md` | The design |
| `docs/setup-and-inputs.md` | Accounts, env vars, inputs still needed |
| `docs/demo-runbook.md` | Running the demo |
| `docs/jev.md` | Notes on the Jev classifier |
| `docs/plans/slice-*.md` | The spec each part was built from (slices 1–10) |
| `CLAUDE.md` | Working rules and all commands |

## Status

**Hackathon prototype.** It works end to end against a Stripe sandbox, Cognee Cloud and Claude, with every outbound email held for human approval.

It is **not** production-ready: there's no hosting or scheduler, no reviewer UI beyond the demo page, and real email sending is untested. See `docs/next-steps.md`.
