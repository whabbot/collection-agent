from datetime import date

import pytest
import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.chasing import InvoiceState, InvoiceStatus, ReminderKind, ReminderSent
from collection_agent.daily import DailyReport, run_daily
from collection_agent.db.models import DailyRun, Invoice, OutboundEmail, OutboundStatus, Reminder
from collection_agent.db.repository import (
    NoApprovedPolicyError,
    load_decision_inputs,
    record_reminder,
)
from tests.api_helpers import AUTH, make_client
from tests.daily_helpers import IDENTITY, THU_8_OCT, URL, approve_default_policy, chased_invoice
from tests.fake_email import FakeEmailSender
from tests.fake_stripe import FakeStripeGateway

pytestmark = pytest.mark.db


def outboxes(session: Session) -> list[OutboundEmail]:
    return list(session.scalars(select(OutboundEmail).order_by(OutboundEmail.created_at)))


def go(
    factory: sessionmaker[Session],
    gateway: FakeStripeGateway,
    today: date = THU_8_OCT,
    **kw: bool,
) -> DailyReport:
    return run_daily(factory, gateway, IDENTITY, today, **kw)


def outcomes(report: DailyReport) -> list[str]:
    return [i.outcome for i in report.invoices]


def test_weekend_is_empty_with_no_stripe_calls_and_no_writes(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    gateway = FakeStripeGateway()

    report = go(session_factory, gateway, date(2026, 10, 10))  # a Saturday

    assert report.invoices == []
    assert gateway.calls == []
    assert session.scalar(select(func.count(DailyRun.id))) == 0
    assert outboxes(session) == []


def test_bank_holiday_is_empty(session: Session, session_factory: sessionmaker[Session]) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    report = go(session_factory, FakeStripeGateway(), date(2026, 12, 25))
    assert report.invoices == []


def test_no_approved_policy_raises(session_factory: sessionmaker[Session]) -> None:
    gateway = FakeStripeGateway()
    with pytest.raises(NoApprovedPolicyError):
        go(session_factory, gateway)
    assert gateway.calls == []


def test_unsent_invoice_is_not_touched(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, sent_on=None)
    report = go(session_factory, FakeStripeGateway())
    assert [i.invoice_id for i in report.invoices] == []
    assert outboxes(session) == []
    assert inv.sent_on is None


def test_due_reminder_is_drafted_with_exact_text(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, reference="INV-9")
    gateway = FakeStripeGateway()

    report = go(session_factory, gateway)

    assert outcomes(report) == ["drafted:reminder[no_response]"]
    assert report.counts == {"drafted:reminder[no_response]": 1}
    assert gateway.methods() == ["get_invoice_status"]
    (row,) = outboxes(session)
    assert row.status is OutboundStatus.PENDING_APPROVAL
    assert row.kind == "reminder"
    assert row.attach_contract is False
    assert row.to_address == "ap@acme.test"
    assert (row.from_address, row.from_name) == ("billing@billing.example.test", "Acme Billing")
    assert row.reply_to == f"reply+{inv.reply_token}@billing.example.test"
    assert row.rule_code == "no_response"
    assert row.rule_label == "No response for 7 days (last contact Thursday 1 October 2026)"
    assert row.subject == "Reminder: invoice INV-9 from Acme Co"
    assert row.body_text == (
        "Hello Acme Ltd,\n\n"
        "Invoice INV-9 for £120.00 is due by Monday 16 November 2026, "
        "and we haven't yet received payment.\n\n"
        f"You can view and pay it here:\n{URL}\n\n"
        "If you've already paid, thank you, and please ignore this email. "
        "If anything is unclear, just reply.\n\n"
        "Thanks,\nAcme Billing\nAcme Co\n"
    )
    assert session.scalar(select(func.count(Reminder.id))) == 0  # recorded on send, not on draft
    (run,) = session.scalars(select(DailyRun)).all()
    assert (run.run_date, run.dry_run, run.counts) == (THU_8_OCT, False, report.counts)
    assert run.started_at <= run.finished_at


def test_nothing_due_is_reported(session: Session, session_factory: sessionmaker[Session]) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    gateway = FakeStripeGateway()
    report = go(session_factory, gateway, date(2026, 10, 7))  # one day early
    assert outcomes(report) == ["nothing_due"]
    assert gateway.calls == []  # Stripe is only consulted when something is due


def test_second_run_same_day_does_not_draft_again(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    gateway = FakeStripeGateway()
    go(session_factory, gateway)
    second = go(session_factory, gateway)

    assert outcomes(second) == ["awaiting_approval"]
    assert len(outboxes(session)) == 1
    assert session.scalar(select(func.count(DailyRun.id))) == 2


def test_a_sending_reminder_also_blocks_a_new_draft(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    go(session_factory, FakeStripeGateway())
    (row,) = outboxes(session)
    row.status = OutboundStatus.SENDING
    session.flush()

    report = go(session_factory, FakeStripeGateway())

    assert outcomes(report) == ["awaiting_approval"]
    assert len(outboxes(session)) == 1


def test_rejected_reminder_is_redrafted_next_run(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    go(session_factory, FakeStripeGateway())
    (row,) = outboxes(session)
    row.status = OutboundStatus.REJECTED
    session.flush()

    assert outcomes(go(session_factory, FakeStripeGateway())) == ["drafted:reminder[no_response]"]


def test_approved_reminder_makes_next_weeks_run_draft_level_two(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, reference="INV-9")
    go(session_factory, FakeStripeGateway())
    (first,) = outboxes(session)
    client = make_client(session=session, email_sender=FakeEmailSender(), today=THU_8_OCT)
    resp = client.post(f"/outbox/{first.id}/approve", json={"approved_by": "sam"}, headers=AUTH)
    assert resp.status_code == 200

    same_day = go(session_factory, FakeStripeGateway())
    next_week = go(session_factory, FakeStripeGateway(), date(2026, 10, 15))

    assert outcomes(same_day) == ["nothing_due"]
    assert outcomes(next_week) == ["drafted:reminder[no_response]"]
    second = outboxes(session)[-1]
    assert second.id != first.id
    assert second.subject == "Second reminder: invoice INV-9"
    assert inv.state is InvoiceState.ACTIVE


def test_squeeze_scenario_drafts_a_pre_deadline_reminder(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    # Sent Thursday 12 Nov; the deadline rolls from Sun 15 Nov to Mon 16 Nov. The 7-day rule
    # would wait until the 19th, so the 3-working-day buffer has to trigger it.
    chased_invoice(session, reference="INV-12", sent_on=date(2026, 11, 12))

    report = go(session_factory, FakeStripeGateway(), date(2026, 11, 13))

    assert outcomes(report) == ["drafted:reminder_pre_deadline[pre_deadline]"]
    (row,) = outboxes(session)
    assert row.kind == "reminder_pre_deadline"
    assert row.rule_code == "pre_deadline"
    assert row.rule_label == (
        "Payment must clear by the 15th \u2014 reminder 3 working days before "
        "Monday 16 November 2026"
    )
    assert (
        "This is a reminder that invoice INV-12 for £120.00 is due by Monday 16 November 2026."
        in row.body_text
    )


def test_broken_promise_names_the_promised_date(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session, promised_date=date(2026, 10, 5))

    report = go(session_factory, FakeStripeGateway())  # grace ends Wed 7 Oct

    assert outcomes(report) == ["drafted:reminder_broken_promise[broken_promise]"]
    (row,) = outboxes(session)
    assert row.kind == "reminder_broken_promise"
    assert row.rule_code == "broken_promise"
    assert row.rule_label == (
        "Promised payment by Monday 5 October 2026 not received (2 working days' grace)"
    )
    assert "by Monday 5 October 2026, but we haven't yet received payment." in row.body_text


def test_max_reminders_then_silence_escalates_without_a_draft(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    for sent in (date(2026, 10, 8), date(2026, 10, 15), date(2026, 10, 22)):
        record_reminder(session, inv.id, ReminderKind.REMINDER, sent)

    report = go(session_factory, FakeStripeGateway(), date(2026, 10, 29))

    assert outcomes(report) == ["escalated[max_reminders]"]
    assert outboxes(session) == []
    session.refresh(inv)
    assert inv.state is InvoiceState.ESCALATED
    assert inv.escalated_at is not None
    assert inv.escalation_rule_code == "max_reminders"
    assert inv.escalation_rule_label == (
        "3 reminders sent without payment \u2014 passed to a person"
    )
    # And it leaves the daily list for good.
    assert outcomes(go(session_factory, FakeStripeGateway(), date(2026, 11, 5))) == []


def test_level_three_is_used_after_two_reminders(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, reference="INV-3")
    record_reminder(session, inv.id, ReminderKind.REMINDER, date(2026, 10, 1))
    record_reminder(session, inv.id, ReminderKind.REMINDER, date(2026, 10, 8))

    go(session_factory, FakeStripeGateway(), date(2026, 10, 15))

    (row,) = outboxes(session)
    assert row.subject == "Invoice INV-3: payment required"
    assert row.body_text.startswith("Dear Acme Ltd,")


@pytest.mark.parametrize(
    ("stripe_status", "outcome", "status", "state"),
    [
        ("paid", "paid_in_stripe", InvoiceStatus.PAID, InvoiceState.ACTIVE),
        ("void", "void_in_stripe", InvoiceStatus.VOID, InvoiceState.ACTIVE),
        ("uncollectible", "uncollectible_in_stripe", InvoiceStatus.OPEN, InvoiceState.WITH_HUMAN),
    ],
)
def test_stripe_says_not_open_updates_the_invoice_and_drafts_nothing(
    session: Session,
    session_factory: sessionmaker[Session],
    stripe_status: str,
    outcome: str,
    status: InvoiceStatus,
    state: InvoiceState,
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    gateway = FakeStripeGateway()
    gateway.statuses[inv.stripe_invoice_id] = stripe_status

    report = go(session_factory, gateway)

    assert outcomes(report) == [outcome]
    assert outboxes(session) == []
    session.refresh(inv)
    assert (inv.status, inv.state) == (status, state)


def test_stripe_paid_also_stops_an_escalation(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    for sent in (date(2026, 10, 8), date(2026, 10, 15), date(2026, 10, 22)):
        record_reminder(session, inv.id, ReminderKind.REMINDER, sent)
    gateway = FakeStripeGateway()
    gateway.statuses[inv.stripe_invoice_id] = "paid"

    report = go(session_factory, gateway, date(2026, 10, 29))

    assert outcomes(report) == ["paid_in_stripe"]
    session.refresh(inv)
    assert inv.state is InvoiceState.ACTIVE
    assert inv.escalated_at is None


def test_unexpected_stripe_status_changes_nothing(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    gateway = FakeStripeGateway()
    gateway.statuses[inv.stripe_invoice_id] = "draft"

    report = go(session_factory, gateway)

    assert outcomes(report) == ["stripe_error"]
    assert report.has_errors
    assert outboxes(session) == []
    session.refresh(inv)
    assert (inv.status, inv.state) == (InvoiceStatus.OPEN, InvoiceState.ACTIVE)


def test_stripe_error_on_one_invoice_does_not_stop_the_others(
    session: Session,
    session_factory: sessionmaker[Session],
    caplog: pytest.LogCaptureFixture,
) -> None:
    approve_default_policy(session)
    bad = chased_invoice(session, sent_on=date(2026, 9, 1), reference="BAD")
    good = chased_invoice(session, reference="GOOD")
    gateway = FakeStripeGateway()
    err = stripe.APIConnectionError(
        "secret-looking detail cus_123 ap@acme.test", headers={"request-id": "req_abc"}
    )
    gateway.status_errors[bad.stripe_invoice_id] = err

    report = go(session_factory, gateway)

    by_ref = {i.reference: i.outcome for i in report.invoices}
    assert by_ref == {"BAD": "stripe_error", "GOOD": "drafted:reminder[no_response]"}
    assert report.has_errors
    (row,) = outboxes(session)
    assert row.invoice_id == good.id
    assert "req_abc" in caplog.text
    assert "secret-looking" not in caplog.text
    assert "ap@acme.test" not in caplog.text


def test_unexpected_error_on_one_invoice_is_isolated(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    broken = chased_invoice(session, sent_on=date(2026, 9, 1), reference="BROKEN")
    broken.hosted_invoice_url = None  # cannot render a reminder
    chased_invoice(session, reference="FINE")
    session.flush()

    report = go(session_factory, FakeStripeGateway())

    assert {i.reference: i.outcome for i in report.invoices} == {
        "BROKEN": "error",
        "FINE": "drafted:reminder[no_response]",
    }
    assert report.has_errors
    assert len(outboxes(session)) == 1


def test_dry_run_writes_nothing_and_never_calls_stripe(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    due = chased_invoice(session, reference="DUE")
    chased_invoice(session, reference="LATER", sent_on=date(2026, 10, 7))
    esc = chased_invoice(session, reference="ESC", sent_on=date(2026, 9, 1))
    for sent in (date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22)):
        record_reminder(session, esc.id, ReminderKind.REMINDER, sent)
    gateway = FakeStripeGateway()

    report = run_daily(session_factory, gateway, None, THU_8_OCT, dry_run=True)

    assert {i.reference: i.outcome for i in report.invoices} == {
        "DUE": "would_drafted:reminder[no_response]",
        "LATER": "nothing_due",
        "ESC": "would_escalated[max_reminders]",
    }
    assert report.dry_run
    assert gateway.calls == []
    assert outboxes(session) == []
    assert session.scalar(select(func.count(DailyRun.id))) == 0
    session.refresh(esc)
    assert (esc.state, esc.escalated_at) == (InvoiceState.ACTIVE, None)
    assert (esc.escalation_rule_code, esc.escalation_rule_label) == (None, None)
    assert due.status is InvoiceStatus.OPEN


def test_dry_run_reports_an_outstanding_draft_as_awaiting_approval(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)
    go(session_factory, FakeStripeGateway())
    report = run_daily(session_factory, None, None, THU_8_OCT, dry_run=True)
    assert outcomes(report) == ["awaiting_approval"]


def test_live_run_needs_gateway_and_identity(session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(RuntimeError):
        run_daily(session_factory, None, IDENTITY, THU_8_OCT)
    with pytest.raises(RuntimeError):
        run_daily(session_factory, FakeStripeGateway(), None, THU_8_OCT)


def test_report_to_dict_is_json_ready(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, reference="R1")
    out = go(session_factory, FakeStripeGateway()).to_dict()
    assert out == {
        "run_date": "2026-10-08",
        "dry_run": False,
        "counts": {"drafted:reminder[no_response]": 1},
        "invoices": [
            {
                "invoice_id": str(inv.id),
                "reference": "R1",
                "outcome": "drafted:reminder[no_response]",
            }
        ],
    }


def test_decide_sees_the_reminder_after_approval(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    go(session_factory, FakeStripeGateway())
    (row,) = outboxes(session)
    client = make_client(session=session, email_sender=FakeEmailSender(), today=THU_8_OCT)
    assert (
        client.post(
            f"/outbox/{row.id}/approve", json={"approved_by": "sam"}, headers=AUTH
        ).status_code
        == 200
    )
    assert load_decision_inputs(session, inv.id).reminders == [
        ReminderSent(kind=ReminderKind.REMINDER, sent_on=THU_8_OCT)
    ]


def test_unique_index_race_counts_as_awaiting_approval(
    session: Session, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process drafted between our check and our insert: the index rejects ours."""
    import collection_agent.daily as daily

    approve_default_policy(session)
    inv = chased_invoice(session)
    go(session_factory, FakeStripeGateway())  # the "other process"
    monkeypatch.setattr(daily, "_has_active_reminder", lambda *_: False)

    report = go(session_factory, FakeStripeGateway())

    assert outcomes(report) == ["awaiting_approval"]
    assert len(outboxes(session)) == 1
    assert isinstance(inv, Invoice)
