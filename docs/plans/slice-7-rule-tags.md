# Slice 7 — Rule tags on reminders and escalations

Status: built and committed to `main` (15 Oct 2026).

## Goal

Every reminder draft and every escalation records **which rule fired**: a stable code plus a human label built from the approved `ChasePolicy` and the invoice's dates. The outbox API returns it so reviewers (and the demo) can see why a chase was drafted, e.g. "No response for 7 days (last contact Mon 2 Nov 2026)". Reminders sent later carry the tag through to the `reminders` row, so a timeline can show it.

## Constraints

- `decide_next_action` stays **unchanged and pure**. The tag comes from a separate pure function that sees the same inputs.
- Labels are built only from the approved policy, the invoice's stored dates and the action. Never from an LLM or Cognee.

## `describe_rule` (new, pure; `src/collection_agent/rules.py`)

`describe_rule(action, inv, policy, reminders, replies, today) -> RuleTag`. `RuleTag` is a frozen dataclass with `code: str` and `label: str`.

| Action | `code` | `label` |
|---|---|---|
| `REMIND` | `no_response` | `No response for {no_response_days} days (last contact {last_contact})` |
| `REMIND_PRE_DEADLINE` | `pre_deadline` | `Payment must clear by the {clearance_day_ordinal} — reminder {buffer} working days before {due_on}` |
| `REMIND_BROKEN_PROMISE` | `broken_promise` | `Promised payment by {promised_date} not received ({grace} working days' grace)` |
| `ESCALATE_TO_HUMAN` | `max_reminders` if `len(reminders) >= max_reminders`, else `broken_promise_at_max` | `{max_reminders} reminders sent without payment — passed to a person` |

Rules for the label values:
- `last_contact` is computed exactly as `decide_next_action` does: the latest of `sent_on`, reminder dates and reply dates.
- Dates use the existing `format_date`.
- `clearance_day_ordinal` renders as 1st, 2nd, 3rd, 4th, …, 11th, 12th, 13th, 21st, 22nd, 23rd, 31st. The policy caps the day at 28, but test the helper up to 31 anyway.
- Raise `ValueError` for an action that has no tag (never `None`).

## Storage (migration `0006`)

- `outbound_emails`: `rule_code` (String, null), `rule_label` (String, null). Set for reminder kinds drafted by the daily run; null for other kinds.
- `reminders`: `rule_code`, `rule_label` (both null). Copied from the outbox row when an approved reminder is recorded (`apply_sent` / resolve).
- `invoices`: `escalation_rule_code`, `escalation_rule_label` (both null). Set together with `state=escalated`.

The migration round-trips.

## Wiring

- `daily.py`: when drafting a reminder or escalating, call `describe_rule` with the same inputs used for `decide_next_action` and store the result. In dry run, add the code to the outcome (`would_drafted:reminder[no_response]`). Normal-run outcomes also gain the `[code]` suffix. Update existing tests that assert outcome strings.
- Outbox responses (`GET /outbox`, `GET /outbox/{id}`, approve/reject/resolve responses) include `rule_code` and `rule_label`.
- `GET /invoices?state=escalated` includes `escalation_rule_code` and `escalation_rule_label`.
- `record_reminder` gains optional `rule_code` / `rule_label` kwargs.

## Tests

- `describe_rule`: every row, with exact labels (default policy). Includes the slice-1 squeeze scenario (`pre_deadline`, due Mon 16 Nov 2026), a reply-reset case (last contact is the reply date), and both escalation codes.
- Ordinal helper: 1–31.
- Consistency: for every slice-1 `decide_next_action` scenario that returns an action, `describe_rule` returns a tag without raising.
- Daily run: the drafted outbox row carries the expected code and label; an escalated invoice carries its escalation tag; dry-run and normal outcome strings include `[code]`.
- Approving a tagged reminder copies the tag to the `reminders` row; resolving a stuck one as `sent` does too.
- API responses include the fields.
- Migration `0006` round-trips.
- All earlier tests still pass.

## Done when

- `docker compose up -d db`, then all of these pass:
  - `uv run pytest`
  - `uv run pytest -m "not db"`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `uv run mypy src`
  - `uv run alembic upgrade head` on the dev DB
- No network calls. `.env` untouched. Nothing committed.
