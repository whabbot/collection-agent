# Slice 1 — project skeleton, `ChasePolicy`, `decide_next_action`

Status: ready to build (15 Oct 2026). Branch: `slice-1-chase-policy`.

This spec **replaces** the `decide_next_action` sketch in `docs/architecture.md`. That sketch has known bugs (repeat pre-deadline reminders every day, escalation the day after the 3rd reminder, no-response clock ignores customer replies). Build to the rules below, not to the sketch.

## Scope

In:
- Python project skeleton managed with `uv`.
- Working-day calendar (weekends + England & Wales bank holidays).
- `ChasePolicy` (Pydantic) with the decided defaults.
- Plain input models for the decision function (no ORM).
- `decide_next_action` — pure function.
- Tests for all of the above.

Out (later slices): FastAPI app, database/SQLAlchemy models, Stripe webhooks, email, reply classifier, approval queue, Cognee, scheduler. Do not add FastAPI/SQLAlchemy/Stripe dependencies yet.

## Project setup

- Python **3.14** (latest stable; `uv python install 3.14`). `requires-python = ">=3.14"`. System `python3` is 3.9 — always go through `uv run`.
- Latest stable versions of every dependency, resolved by `uv add` (don't hand-pin old versions).
- Runtime deps: `pydantic` (v2), `holidays` (for building the UK calendar only).
- Dev deps: `pytest`, `ruff`, `mypy` (strict on `src/`).
- Layout: `src/collection_agent/` package (src layout), `tests/`.
  - `src/collection_agent/calendar.py` — working-day calendar
  - `src/collection_agent/policy.py` — `ChasePolicy`
  - `src/collection_agent/chasing.py` — input models, `Action`, `decide_next_action`
- Commands that must pass: `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`.
- Fill in the empty `## Commands` section of `CLAUDE.md` with these.

## Working-day calendar

`WorkingCalendar` — an immutable object built from a set of holiday dates. Weekends (Sat/Sun) are never working days.

- `is_working_day(d: date) -> bool`
- `roll_forward(d: date) -> date` — `d` if working, else the next working day.
- `add_working_days(d: date, n: int) -> date` — move `n` working days forward (`n > 0`) or back (`n < 0`), counting only working days.
- `uk_calendar(years: Iterable[int]) -> WorkingCalendar` — factory using the `holidays` library, England (`subdiv="ENG"`, which covers England & Wales bank holidays). Only this factory touches the library; the decision function receives a ready calendar.

## `ChasePolicy`

Pydantic model, frozen. Fields and defaults (decided — see `docs/decisions.md`):

| Field | Default | Meaning |
|---|---|---|
| `no_response_days` | 7 | Calendar days of silence after the last contact before a reminder is due |
| `clearance_day_of_month` | 15 | Day payment must clear by |
| `clearance_month_offset` | 1 | Months after the month the invoice was sent (1 = following month) |
| `pre_deadline_buffer_working_days` | 3 | Pre-deadline window opens this many working days before the clearance deadline |
| `max_reminders` | 3 | Reminders (of any kind) before escalation |
| `promise_grace_working_days` | 2 | Working days after a promised date before the promise counts as broken |

Validate: all positive; `clearance_day_of_month` 1–28 (avoids month-length edge cases).

Method: `clearance_deadline(sent_on: date, cal: WorkingCalendar) -> date` = `cal.roll_forward(date(year, month, day))` where year/month are `sent_on`'s month plus `clearance_month_offset` (handle the December → January rollover).

## Input models (plain Pydantic, frozen)

```python
class InvoiceStatus(StrEnum):
    OPEN, PAID, VOID


class InvoiceState(StrEnum):
    ACTIVE, DISPUTED, WITH_HUMAN, ESCALATED


class InvoiceSnapshot:
    id: str
    status: InvoiceStatus
    state: InvoiceState
    sent_on: date  # date the invoice email went out (UK local date)
    paused_until: date | None
    promised_date: date | None


class ReminderKind(StrEnum):
    REMINDER, PRE_DEADLINE, BROKEN_PROMISE


class ReminderSent:  # outbound chase emails (not the initial invoice email)
    kind: ReminderKind
    sent_on: date


class CustomerReply:  # inbound replies that count as a response (out-of-office already filtered out upstream)
    received_on: date
```

```python
class Action(StrEnum):
    REMIND = "remind"
    REMIND_PRE_DEADLINE = "remind_pre_deadline"
    REMIND_BROKEN_PROMISE = "remind_broken_promise"
    ESCALATE_TO_HUMAN = "escalate_to_human"
```

## `decide_next_action`

```python
def decide_next_action(
    inv: InvoiceSnapshot,
    policy: ChasePolicy,
    reminders: Sequence[ReminderSent],
    replies: Sequence[CustomerReply],
    today: date,
    cal: WorkingCalendar,
) -> Action | None
```

Pure: no I/O, no clock reads, no randomness, no LLM, no Cognee. Doesn't assume inputs are sorted.

Definitions:
- `clear_by = policy.clearance_deadline(inv.sent_on, cal)`
- `last_contact` = latest of `inv.sent_on`, any reminder's `sent_on`, any reply's `received_on`.
- `window_start = cal.add_working_days(clear_by, -policy.pre_deadline_buffer_working_days)`

Rules, evaluated **in this order**; first match wins:

1. `inv.status` is PAID or VOID → `None`.
2. `inv.state` is DISPUTED, WITH_HUMAN or ESCALATED → `None`.
3. `today` is not a working day → `None` (no sends on weekends/bank holidays; whatever was due fires on the next working day).
4. `inv.paused_until` is set and `today < paused_until` → `None`.
5. **Max reminders reached** (`len(reminders) >= max_reminders`):
   - if a promise exists and is broken (rule 6's condition) → `ESCALATE_TO_HUMAN`;
   - else if `today >= last_contact + no_response_days` → `ESCALATE_TO_HUMAN`;
   - else `None`. (So escalation waits a full no-response period after the last reminder, and a customer reply resets that wait.)
6. **Broken promise**: `inv.promised_date` is set, `today > cal.add_working_days(promised_date, promise_grace_working_days)`, and no BROKEN_PROMISE reminder has been sent on or after `promised_date` → `REMIND_BROKEN_PROMISE`. Fires at most once per promised date.
7. **Pre-deadline**: `window_start <= today <= clear_by` and **no reminder of any kind** has been sent on or after `window_start` → `REMIND_PRE_DEADLINE`. (At most one reminder inside the window; a regular reminder that already landed in the window counts.)
8. **No response**: `today >= last_contact + timedelta(days=no_response_days)` → `REMIND`.
9. Otherwise `None`.

After `clear_by` passes, rule 8 keeps chasing every 7 days of silence until rule 5 escalates.

## Required tests

Use explicit holiday sets in decision tests (deterministic); test `uk_calendar` separately. Dates below are real 2026 weekdays — keep them.

Calendar / policy:
- `roll_forward` over a weekend and over a bank holiday; `add_working_days` forward and back across a weekend and Christmas (25 Dec 2026 Fri, 28 Dec 2026 Mon is the substitute Boxing Day holiday).
- `uk_calendar([2026])` treats 25 Dec 2026 and 28 Dec 2026 as non-working.
- `clearance_deadline`: sent 12 Oct 2026 → 15 Nov 2026 is a Sunday → **Mon 16 Nov 2026**. Sent in December 2026 → 15 Jan 2027 (Fri). Validation rejects day 0 / 29 and non-positive numbers.

Decision function (default policy unless stated):
- **Squeeze case** (the must-have): invoice sent Thu 29 Oct 2026, `clear_by` = Mon 16 Nov, `window_start` = Wed 11 Nov. Reminder sent Thu 5 Nov. On Wed 11 Nov → `REMIND_PRE_DEADLINE` (the 7-day timer would only fire on 12 Nov). With that pre-deadline reminder recorded on 11 Nov, Thu 12 Nov → `None`.
- **Pre-deadline fires once**: after a pre-deadline reminder, every remaining day in the window → `None`.
- **Regular reminder inside the window counts**: a REMIND sent on 12 Nov (inside window) → no pre-deadline reminder on 13 Nov.
- **7-day rule**: sent Mon 12 Oct → `None` on 18 Oct, `REMIND` on Mon 19 Oct.
- **7-day due on a weekend** → `None` on Sat/Sun, `REMIND` on Monday.
- **Customer reply resets the clock**: reply received 3 days after a reminder pushes the next reminder to reply + 7.
- **Bank holiday**: due date on a holiday → `None`, fires the next working day.
- **Escalation waits**: 3 reminders sent, the 3rd on day D → `None` on D+1 … D+6, `ESCALATE_TO_HUMAN` on D+7 (or next working day).
- **Escalation reset by reply**: reply after the 3rd reminder delays escalation to reply + 7.
- **Broken promise**: promised Fri 6 Nov 2026, grace 2 working days → Tue 10 Nov; `None` on 10 Nov, `REMIND_BROKEN_PROMISE` on Wed 11 Nov; not repeated after one is recorded.
- **Broken promise at max reminders** → `ESCALATE_TO_HUMAN`.
- **Paused**: `None` while `today < paused_until`; normal rules from `paused_until` on.
- **Terminal states**: PAID, VOID, DISPUTED, WITH_HUMAN, ESCALATED → `None` even when a reminder is overdue.
- **Order-independence**: shuffled `reminders`/`replies` give the same result.

## Done when

- All four commands above pass.
- No network access, secrets or Cognee writes anywhere.
- `CLAUDE.md` `## Commands` section filled in.
- Changes left **uncommitted** on branch `slice-1-chase-policy` for review.

## Open points (don't block; flag if you hit them)

- Should a contract-specific due date override the policy deadline? For now the policy deadline is the only deadline.
- Does a customer reply that's a dispute/promise matter here? Not in this slice — those change `state` / `promised_date` / `paused_until` upstream.
