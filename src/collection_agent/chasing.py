"""Pure chase-timing decision. No I/O, no clock reads, no LLM."""

from collections.abc import Sequence
from datetime import date, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from collection_agent.calendar import WorkingCalendar
from collection_agent.policy import ChasePolicy


class InvoiceStatus(StrEnum):
    OPEN = "open"
    PAID = "paid"
    VOID = "void"


class InvoiceState(StrEnum):
    ACTIVE = "active"
    DISPUTED = "disputed"
    WITH_HUMAN = "with_human"
    ESCALATED = "escalated"


class ReminderKind(StrEnum):
    REMINDER = "reminder"
    PRE_DEADLINE = "pre_deadline"
    BROKEN_PROMISE = "broken_promise"


class Action(StrEnum):
    REMIND = "remind"
    REMIND_PRE_DEADLINE = "remind_pre_deadline"
    REMIND_BROKEN_PROMISE = "remind_broken_promise"
    ESCALATE_TO_HUMAN = "escalate_to_human"


class InvoiceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    status: InvoiceStatus
    state: InvoiceState
    sent_on: date
    paused_until: date | None = None
    promised_date: date | None = None


class ReminderSent(BaseModel):
    """An outbound chase email (not the initial invoice email)."""

    model_config = ConfigDict(frozen=True)

    kind: ReminderKind
    sent_on: date


class CustomerReply(BaseModel):
    """An inbound reply that counts as a response (out-of-office filtered upstream)."""

    model_config = ConfigDict(frozen=True)

    received_on: date


_TERMINAL_STATUSES = frozenset({InvoiceStatus.PAID, InvoiceStatus.VOID})
_TERMINAL_STATES = frozenset(
    {InvoiceState.DISPUTED, InvoiceState.WITH_HUMAN, InvoiceState.ESCALATED}
)


def decide_next_action(
    inv: InvoiceSnapshot,
    policy: ChasePolicy,
    reminders: Sequence[ReminderSent],
    replies: Sequence[CustomerReply],
    today: date,
    cal: WorkingCalendar,
) -> Action | None:
    """Return the chase action due today, or None. Rules are evaluated in order."""
    if inv.status in _TERMINAL_STATUSES or inv.state in _TERMINAL_STATES:
        return None
    if not cal.is_working_day(today):
        return None
    if inv.paused_until is not None and today < inv.paused_until:
        return None

    clear_by = policy.clearance_deadline(inv.sent_on, cal)
    last_contact = max(
        [inv.sent_on, *(r.sent_on for r in reminders), *(r.received_on for r in replies)]
    )
    window_start = cal.add_working_days(clear_by, -policy.pre_deadline_buffer_working_days)
    silent_period_over = today >= last_contact + timedelta(days=policy.no_response_days)

    promise_broken = (
        inv.promised_date is not None
        and today > cal.add_working_days(inv.promised_date, policy.promise_grace_working_days)
        and not any(
            r.kind is ReminderKind.BROKEN_PROMISE and r.sent_on >= inv.promised_date
            for r in reminders
        )
    )

    if len(reminders) >= policy.max_reminders:
        if promise_broken or silent_period_over:
            return Action.ESCALATE_TO_HUMAN
        return None

    if promise_broken:
        return Action.REMIND_BROKEN_PROMISE

    if window_start <= today <= clear_by and not any(r.sent_on >= window_start for r in reminders):
        return Action.REMIND_PRE_DEADLINE

    if silent_period_over:
        return Action.REMIND

    return None
