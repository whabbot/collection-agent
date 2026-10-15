"""Approving and resolving reminder emails, and the escalations list."""

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.chasing import InvoiceState, ReminderKind, ReminderSent
from collection_agent.daily import run_daily
from collection_agent.db.models import (
    Invoice,
    OutboundEmail,
    OutboundKind,
    OutboundStatus,
    Reminder,
)
from collection_agent.db.repository import load_decision_inputs, record_reminder
from collection_agent.email_sender import EmailSendError
from tests.api_helpers import AUTH, make_client
from tests.daily_helpers import IDENTITY, THU_8_OCT, approve_default_policy, chased_invoice
from tests.fake_email import FakeEmailSender
from tests.fake_stripe import FakeStripeGateway

pytestmark = pytest.mark.db


def setup(session: Session, today: date = THU_8_OCT) -> tuple[TestClient, FakeEmailSender]:
    sender = FakeEmailSender()
    return make_client(session=session, email_sender=sender, today=today), sender


def approve(client: TestClient, outbox_id: object) -> int:
    return client.post(
        f"/outbox/{outbox_id}/approve", json={"approved_by": "sam"}, headers=AUTH
    ).status_code


def resolve(client: TestClient, outbox_id: object, **body: object) -> object:
    payload = {"resolved_by": "sam", "outcome": "sent", **body}
    return client.post(f"/outbox/{outbox_id}/resolve", json=payload, headers=AUTH)


def draft_reminder(
    session: Session, factory: sessionmaker[Session], **kw: object
) -> tuple[Invoice, OutboundEmail]:
    approve_default_policy(session)
    inv = chased_invoice(session, **kw)
    run_daily(factory, FakeStripeGateway(), IDENTITY, THU_8_OCT)
    row = session.scalars(select(OutboundEmail).where(OutboundEmail.invoice_id == inv.id)).one()
    return inv, row


def stuck(session: Session, inv: Invoice, kind: OutboundKind) -> OutboundEmail:
    row = OutboundEmail(
        invoice_id=inv.id,
        kind=kind.value,
        to_address="a@b.test",
        from_address="f@b.test",
        from_name="F",
        reply_to="r@b.test",
        subject="s",
        body_text="b",
        attach_contract=False,
        status=OutboundStatus.SENDING,
    )
    session.add(row)
    session.commit()  # the 409/404 paths roll back, which must not discard this setup
    return row


# --- approve ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("promised", "sent_on", "kind", "recorded"),
    [
        (None, date(2026, 10, 1), "reminder", ReminderKind.REMINDER),
        (
            date(2026, 10, 5),
            date(2026, 10, 1),
            "reminder_broken_promise",
            ReminderKind.BROKEN_PROMISE,
        ),
    ],
)
def test_approving_a_reminder_sends_it_and_records_it(
    session: Session,
    session_factory: sessionmaker[Session],
    promised: date | None,
    sent_on: date,
    kind: str,
    recorded: ReminderKind,
) -> None:
    inv, row = draft_reminder(session, session_factory, promised_date=promised, sent_on=sent_on)
    assert row.kind == kind
    client, sender = setup(session)

    assert approve(client, row.id) == 200

    (msg,) = sender.sent
    assert msg.attachments == ()
    assert (msg.subject, msg.text_body) == (row.subject, row.body_text)
    assert msg.reply_to == f"reply+{inv.reply_token}@billing.example.test"
    session.refresh(row)
    assert row.status is OutboundStatus.SENT
    (rem,) = session.scalars(select(Reminder)).all()
    assert (rem.invoice_id, rem.kind, rem.sent_on) == (inv.id, recorded, THU_8_OCT)


def test_pre_deadline_reminder_records_pre_deadline_kind(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, sent_on=date(2026, 11, 12))
    run_daily(session_factory, FakeStripeGateway(), IDENTITY, date(2026, 11, 13))
    row = session.scalars(select(OutboundEmail)).one()
    client, _ = setup(session, date(2026, 11, 13))

    assert approve(client, row.id) == 200

    assert load_decision_inputs(session, inv.id).reminders == [
        ReminderKind.PRE_DEADLINE
        and ReminderSent(kind=ReminderKind.PRE_DEADLINE, sent_on=date(2026, 11, 13))
    ]


def test_reminder_is_recorded_with_the_london_date(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    _, row = draft_reminder(session, session_factory)
    client, _ = setup(session, date(2026, 10, 9))  # the injected London "today"
    assert approve(client, row.id) == 200
    assert session.scalars(select(Reminder.sent_on)).one() == date(2026, 10, 9)


def test_failed_reminder_send_records_nothing(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    _, row = draft_reminder(session, session_factory)
    client, sender = setup(session)
    sender.fail_with = EmailSendError("rejected", error_code=406)

    assert approve(client, row.id) == 502

    assert session.scalars(select(Reminder)).all() == []


def test_unknown_outcome_reminder_stays_sending_records_nothing_and_blocks_redraft(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    inv, row = draft_reminder(session, session_factory)
    client, sender = setup(session)
    sender.fail_with = EmailSendError("no usable reply", outcome_unknown=True)

    assert approve(client, row.id) == 502

    session.refresh(row)
    assert row.status is OutboundStatus.SENDING
    assert session.scalars(select(Reminder)).all() == []
    again = run_daily(session_factory, FakeStripeGateway(), IDENTITY, THU_8_OCT)
    assert [i.outcome for i in again.invoices] == ["awaiting_approval"]
    assert inv.id == row.invoice_id


def test_approving_a_reminder_for_a_paid_invoice_rejects_it(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    inv, row = draft_reminder(session, session_factory)
    inv.status = type(inv.status).PAID
    session.flush()
    client, sender = setup(session)

    assert approve(client, row.id) == 409

    assert sender.sent == []
    assert session.scalars(select(Reminder)).all() == []


# --- resolve ------------------------------------------------------------------------------


def test_resolve_sent_on_a_stuck_invoice_email_sets_sent_on(session: Session) -> None:
    inv = chased_invoice(session, sent_on=None)
    row = stuck(session, inv, OutboundKind.INVOICE)
    client, sender = setup(session)

    resp = resolve(client, row.id, provider_message_id="pm-123")

    assert resp.status_code == 200  # type: ignore[attr-defined]
    out = resp.json()  # type: ignore[attr-defined]
    assert (out["status"], out["provider_message_id"]) == ("sent", "pm-123")
    session.refresh(row)
    session.refresh(inv)
    assert row.sent_at is not None
    assert (row.resolved_by, row.resolved_at is not None) == ("sam", True)
    assert inv.sent_on == THU_8_OCT
    assert session.scalars(select(Reminder)).all() == []
    assert sender.sent == []  # resolving never sends


def test_resolve_sent_keeps_an_existing_sent_on(session: Session) -> None:
    inv = chased_invoice(session)
    row = stuck(session, inv, OutboundKind.INVOICE)
    client, _ = setup(session)
    assert resolve(client, row.id).status_code == 200  # type: ignore[attr-defined]
    assert inv.sent_on == date(2026, 10, 1)


@pytest.mark.parametrize(
    ("kind", "recorded"),
    [
        (OutboundKind.REMINDER, ReminderKind.REMINDER),
        (OutboundKind.REMINDER_PRE_DEADLINE, ReminderKind.PRE_DEADLINE),
        (OutboundKind.REMINDER_BROKEN_PROMISE, ReminderKind.BROKEN_PROMISE),
    ],
)
def test_resolve_sent_on_a_stuck_reminder_records_it(
    session: Session, kind: OutboundKind, recorded: ReminderKind
) -> None:
    inv = chased_invoice(session)
    row = stuck(session, inv, kind)
    client, _ = setup(session)

    assert resolve(client, row.id).status_code == 200  # type: ignore[attr-defined]

    (rem,) = session.scalars(select(Reminder)).all()
    assert (rem.invoice_id, rem.kind, rem.sent_on) == (inv.id, recorded, THU_8_OCT)
    assert load_decision_inputs(session, inv.id).reminders == [
        ReminderSent(kind=recorded, sent_on=THU_8_OCT)
    ]


def test_resolve_failed_frees_the_slot_and_records_nothing(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    inv, row = draft_reminder(session, session_factory)
    row.status = OutboundStatus.SENDING
    session.flush()
    client, _ = setup(session)

    resp = resolve(client, row.id, outcome="failed")

    assert resp.status_code == 200  # type: ignore[attr-defined]
    session.refresh(row)
    assert row.status is OutboundStatus.FAILED
    assert (row.resolved_by, row.resolved_at is not None) == ("sam", True)
    assert session.scalars(select(Reminder)).all() == []
    assert inv.state is InvoiceState.ACTIVE
    again = run_daily(session_factory, FakeStripeGateway(), IDENTITY, THU_8_OCT)
    assert [i.outcome for i in again.invoices] == ["drafted:reminder"]


@pytest.mark.parametrize(
    "status",
    [
        OutboundStatus.PENDING_APPROVAL,
        OutboundStatus.SENT,
        OutboundStatus.FAILED,
        OutboundStatus.REJECTED,
    ],
)
def test_resolve_only_from_sending(session: Session, status: OutboundStatus) -> None:
    inv = chased_invoice(session)
    row = stuck(session, inv, OutboundKind.REMINDER)
    row.status = status
    session.commit()
    client, _ = setup(session)

    resp = resolve(client, row.id)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    assert resp.json() == {"error": "not_sending"}  # type: ignore[attr-defined]
    session.refresh(row)
    assert row.status is status
    assert session.scalars(select(Reminder)).all() == []


def test_resolve_unknown_404(session: Session) -> None:
    client, _ = setup(session)
    assert resolve(client, uuid.uuid4()).status_code == 404  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"resolved_by": "", "outcome": "sent"},
        {"resolved_by": "sam", "outcome": "maybe"},
        {"resolved_by": "sam", "outcome": "failed", "provider_message_id": "pm-1"},
        {"resolved_by": "sam", "outcome": "sent", "extra": 1},
    ],
)
def test_resolve_body_validation_422(session: Session, body: dict[str, object]) -> None:
    inv = chased_invoice(session)
    row = stuck(session, inv, OutboundKind.REMINDER)
    client, _ = setup(session)
    resp = client.post(f"/outbox/{row.id}/resolve", json=body, headers=AUTH)
    assert resp.status_code == 422
    session.refresh(row)
    assert row.status is OutboundStatus.SENDING


# --- escalations list ---------------------------------------------------------------------


def test_escalations_list_filters_and_orders_newest_first(session: Session) -> None:
    client, _ = setup(session)
    older = chased_invoice(session, reference="OLD")
    newer = chased_invoice(session, reference="NEW")
    chased_invoice(session, reference="ACTIVE")
    for inv, when in (
        (older, datetime(2026, 10, 1, tzinfo=UTC)),
        (newer, datetime(2026, 10, 5, tzinfo=UTC)),
    ):
        inv.state = InvoiceState.ESCALATED
        inv.escalated_at = when
    record_reminder(session, newer.id, ReminderKind.REMINDER, date(2026, 10, 8))
    record_reminder(session, newer.id, ReminderKind.REMINDER, date(2026, 10, 15))
    session.flush()

    resp = client.get("/invoices", params={"state": "escalated"}, headers=AUTH)

    assert resp.status_code == 200
    rows = resp.json()
    assert [r["reference"] for r in rows] == ["NEW", "OLD"]
    assert rows[0] == {
        "id": str(newer.id),
        "reference": "NEW",
        "customer_name": "Acme Ltd",
        "customer_email": "ap@acme.test",
        "amount_minor": 12000,
        "currency": "GBP",
        "due_on": "2026-11-16",
        "escalated_at": "2026-10-05T00:00:00Z",
        "reminder_count": 2,
    }
    assert rows[1]["reminder_count"] == 0


def test_escalations_list_only_supports_the_escalated_filter(session: Session) -> None:
    client, _ = setup(session)
    assert client.get("/invoices", headers=AUTH).status_code == 422
    assert client.get("/invoices", params={"state": "active"}, headers=AUTH).status_code == 422
