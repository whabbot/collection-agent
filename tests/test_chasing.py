import random
from datetime import date, timedelta

import pytest

from collection_agent.calendar import WorkingCalendar
from collection_agent.chasing import (
    Action,
    CustomerReply,
    InvoiceSnapshot,
    InvoiceState,
    InvoiceStatus,
    ReminderKind,
    ReminderSent,
    decide_next_action,
)
from collection_agent.policy import ChasePolicy

# Explicit holiday set: Christmas 2026 plus a mid-month test holiday on Mon 26 Oct.
CAL = WorkingCalendar(frozenset({date(2026, 12, 25), date(2026, 12, 28), date(2026, 10, 26)}))
POLICY = ChasePolicy()


def inv(
    sent_on: date,
    *,
    status: InvoiceStatus = InvoiceStatus.OPEN,
    state: InvoiceState = InvoiceState.ACTIVE,
    paused_until: date | None = None,
    promised_date: date | None = None,
) -> InvoiceSnapshot:
    return InvoiceSnapshot(
        id="inv_1",
        status=status,
        state=state,
        sent_on=sent_on,
        paused_until=paused_until,
        promised_date=promised_date,
    )


def rem(d: date, kind: ReminderKind = ReminderKind.REMINDER) -> ReminderSent:
    return ReminderSent(kind=kind, sent_on=d)


def reply(d: date) -> CustomerReply:
    return CustomerReply(received_on=d)


def decide(
    i: InvoiceSnapshot,
    today: date,
    reminders: list[ReminderSent] | None = None,
    replies: list[CustomerReply] | None = None,
    policy: ChasePolicy = POLICY,
) -> Action | None:
    return decide_next_action(i, policy, reminders or [], replies or [], today, CAL)


# --- squeeze / pre-deadline -------------------------------------------------

SQ = date(2026, 10, 29)  # Thu; clear_by Mon 16 Nov, window_start Wed 11 Nov


def test_squeeze_case_pre_deadline_beats_seven_day_timer() -> None:
    i = inv(SQ)
    r = [rem(date(2026, 11, 5))]
    assert decide(i, date(2026, 11, 10), r) is None
    assert decide(i, date(2026, 11, 11), r) is Action.REMIND_PRE_DEADLINE


def test_squeeze_case_after_pre_deadline_recorded() -> None:
    i = inv(SQ)
    r = [rem(date(2026, 11, 5)), rem(date(2026, 11, 11), ReminderKind.PRE_DEADLINE)]
    assert decide(i, date(2026, 11, 12), r) is None


def test_pre_deadline_fires_once() -> None:
    i = inv(SQ)
    r = [rem(date(2026, 11, 5)), rem(date(2026, 11, 11), ReminderKind.PRE_DEADLINE)]
    d = date(2026, 11, 12)
    while d <= date(2026, 11, 16):
        assert decide(i, d, r) is None, d
        d += timedelta(days=1)


def test_regular_reminder_inside_window_counts() -> None:
    i = inv(SQ)
    r = [rem(date(2026, 11, 12))]
    assert decide(i, date(2026, 11, 13), r) is None


def test_pre_deadline_not_before_window() -> None:
    assert decide(inv(SQ), date(2026, 11, 9), [rem(date(2026, 11, 5))]) is None


# --- seven-day rule ----------------------------------------------------------


def test_seven_day_rule() -> None:
    i = inv(date(2026, 10, 12))
    assert decide(i, date(2026, 10, 18)) is None
    assert decide(i, date(2026, 10, 19)) is Action.REMIND


def test_seven_day_due_on_weekend_fires_monday() -> None:
    i = inv(date(2026, 10, 10))  # Sat sent_on; due Sat 17 Oct
    assert decide(i, date(2026, 10, 17)) is None  # Sat
    assert decide(i, date(2026, 10, 18)) is None  # Sun
    assert decide(i, date(2026, 10, 19)) is Action.REMIND  # Mon


def test_customer_reply_resets_clock() -> None:
    i = inv(date(2026, 10, 1))
    r = [rem(date(2026, 10, 8))]
    rp = [reply(date(2026, 10, 11))]
    assert decide(i, date(2026, 10, 15), r) is Action.REMIND  # sanity: no reply
    assert decide(i, date(2026, 10, 15), r, rp) is None
    assert decide(i, date(2026, 10, 17), r, rp) is None  # Sat
    assert decide(i, date(2026, 10, 18), r, rp) is None  # Sun, reply+7
    assert decide(i, date(2026, 10, 19), r, rp) is Action.REMIND


def test_bank_holiday_defers_to_next_working_day() -> None:
    i = inv(date(2026, 10, 19))  # due Mon 26 Oct (test holiday)
    assert decide(i, date(2026, 10, 26)) is None
    assert decide(i, date(2026, 10, 27)) is Action.REMIND


# --- escalation ---------------------------------------------------------------


def three_reminders(last: date) -> list[ReminderSent]:
    return [rem(last - timedelta(days=14)), rem(last - timedelta(days=7)), rem(last)]


def test_escalation_waits_full_period() -> None:
    i = inv(date(2026, 9, 1))
    d = date(2026, 10, 5)  # Mon
    r = three_reminders(d)
    for n in range(1, 7):
        assert decide(i, d + timedelta(days=n), r) is None, n
    assert decide(i, d + timedelta(days=7), r) is Action.ESCALATE_TO_HUMAN


def test_escalation_waits_for_next_working_day() -> None:
    i = inv(date(2026, 9, 1))
    d = date(2026, 10, 10)  # Sat; +7 = Sat 17
    r = three_reminders(d)
    assert decide(i, date(2026, 10, 17), r) is None
    assert decide(i, date(2026, 10, 19), r) is Action.ESCALATE_TO_HUMAN


def test_escalation_reset_by_reply() -> None:
    i = inv(date(2026, 9, 1))
    d = date(2026, 10, 5)
    r = three_reminders(d)
    rp = [reply(date(2026, 10, 8))]
    assert decide(i, date(2026, 10, 12), r, rp) is None  # d+7 but reply resets
    assert decide(i, date(2026, 10, 14), r, rp) is None
    assert decide(i, date(2026, 10, 15), r, rp) is Action.ESCALATE_TO_HUMAN


def test_no_regular_reminder_once_max_reached() -> None:
    i = inv(date(2026, 9, 1))
    r = three_reminders(date(2026, 10, 5))
    assert decide(i, date(2026, 10, 9), r) is None


# --- broken promise -----------------------------------------------------------


def test_broken_promise() -> None:
    promised = date(2026, 11, 6)  # Fri; grace 2 wd -> Tue 10 Nov
    i = inv(date(2026, 10, 30), promised_date=promised)
    r = [rem(date(2026, 11, 5))]
    assert decide(i, date(2026, 11, 10), r) is None
    assert decide(i, date(2026, 11, 11), r) is Action.REMIND_BROKEN_PROMISE


def test_broken_promise_not_repeated() -> None:
    promised = date(2026, 11, 6)
    i = inv(date(2026, 10, 30), promised_date=promised)
    r = [rem(date(2026, 11, 5)), rem(date(2026, 11, 11), ReminderKind.BROKEN_PROMISE)]
    assert decide(i, date(2026, 11, 12), r) is None


def test_broken_promise_reminder_before_promised_date_does_not_count() -> None:
    promised = date(2026, 11, 6)
    i = inv(date(2026, 10, 30), promised_date=promised)
    r = [rem(date(2026, 11, 5), ReminderKind.BROKEN_PROMISE)]
    assert decide(i, date(2026, 11, 11), r) is Action.REMIND_BROKEN_PROMISE


def test_broken_promise_at_max_reminders_escalates() -> None:
    promised = date(2026, 11, 6)
    i = inv(date(2026, 10, 1), promised_date=promised)
    r = [rem(date(2026, 10, 8)), rem(date(2026, 10, 15)), rem(date(2026, 11, 5))]
    assert decide(i, date(2026, 11, 11), r) is Action.ESCALATE_TO_HUMAN


def test_broken_promise_takes_precedence_over_pre_deadline() -> None:
    promised = date(2026, 11, 6)
    i = inv(SQ, promised_date=promised)
    assert decide(i, date(2026, 11, 11), [rem(date(2026, 11, 5))]) is Action.REMIND_BROKEN_PROMISE


# --- paused / terminal / order -----------------------------------------------


def test_paused_until() -> None:
    i = inv(date(2026, 10, 1), paused_until=date(2026, 10, 20))
    assert decide(i, date(2026, 10, 19)) is None
    assert decide(i, date(2026, 10, 20)) is Action.REMIND


@pytest.mark.parametrize(
    ("status", "state"),
    [
        (InvoiceStatus.PAID, InvoiceState.ACTIVE),
        (InvoiceStatus.VOID, InvoiceState.ACTIVE),
        (InvoiceStatus.OPEN, InvoiceState.DISPUTED),
        (InvoiceStatus.OPEN, InvoiceState.WITH_HUMAN),
        (InvoiceStatus.OPEN, InvoiceState.ESCALATED),
    ],
)
def test_terminal_states(status: InvoiceStatus, state: InvoiceState) -> None:
    i = inv(date(2026, 10, 1), status=status, state=state)
    assert decide(i, date(2026, 10, 19)) is None
    # also with max reminders exhausted
    assert decide(i, date(2026, 10, 30), three_reminders(date(2026, 10, 10))) is None


def test_overdue_without_terminal_state_acts() -> None:
    assert decide(inv(date(2026, 10, 1)), date(2026, 10, 19)) is Action.REMIND


def test_order_independence() -> None:
    i = inv(date(2026, 9, 1))
    r = [rem(date(2026, 9, 8)), rem(date(2026, 9, 15)), rem(date(2026, 10, 5))]
    rp = [reply(date(2026, 9, 10)), reply(date(2026, 10, 8)), reply(date(2026, 9, 20))]
    expected = decide(i, date(2026, 10, 15), r, rp)
    assert expected is Action.ESCALATE_TO_HUMAN
    rng = random.Random(1)
    for _ in range(20):
        r2, rp2 = r[:], rp[:]
        rng.shuffle(r2)
        rng.shuffle(rp2)
        assert decide(i, date(2026, 10, 15), r2, rp2) == expected
        assert decide(i, date(2026, 10, 14), r2, rp2) is None
