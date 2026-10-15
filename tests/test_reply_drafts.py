"""Approving reply drafts: threading on send, no reminder side effects, status-by-kind checks."""

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.classifier import Intent
from collection_agent.db.models import (
    Invoice,
    OutboundEmail,
    OutboundStatus,
    Reminder,
)
from tests.api_helpers import AUTH, make_client
from tests.fake_classifier import FakeClassifier, make_classification
from tests.fake_email import FakeEmailSender
from tests.fake_stripe import FakeStripeGateway
from tests.inbound_helpers import BASIC, headers, payload
from tests.routing_helpers import drafts, make_message, open_invoice, run_route

pytestmark = pytest.mark.db


def approve(client: TestClient, outbox_id: object) -> object:
    return client.post(f"/outbox/{outbox_id}/approve", json={"approved_by": "sam"}, headers=AUTH)


def drafted(session: Session, intent: Intent, inv: Invoice | None = None) -> OutboundEmail:
    inv = inv or open_invoice(session)
    msg = make_message(session, inv, references="<earlier@x>")
    gateway = FakeStripeGateway()
    gateway.statuses[inv.stripe_invoice_id] = "paid"
    run_route(session, inv, msg, make_classification(intent), gateway=gateway)
    [row] = drafts(session)
    return row


def test_approving_a_reply_sends_it_with_threading_headers_and_records_no_reminder(
    session: Session,
) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    row = drafted(session, Intent.DISPUTE)
    inv = session.get(Invoice, row.invoice_id)
    assert inv is not None

    resp = approve(client, row.id)

    assert resp.status_code == 200  # type: ignore[attr-defined]
    [sent] = sender.sent
    assert sent.headers == (
        ("In-Reply-To", "<reply-1@mail.acme.test>"),
        ("References", "<earlier@x> <reply-1@mail.acme.test>"),
    )
    assert sent.subject == "Re: Invoice INV-1"
    assert sent.reply_to == f"reply+{inv.reply_token}@billing.example.test"
    session.refresh(row)
    assert row.status is OutboundStatus.SENT
    assert session.scalar(select(func.count()).select_from(Reminder)) == 0
    session.refresh(inv)
    assert inv.sent_on == date(2026, 10, 1)  # untouched


def test_holding_reply_is_approvable_though_the_invoice_is_now_disputed(session: Session) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    row = drafted(session, Intent.DISPUTE)
    inv = session.get(Invoice, row.invoice_id)
    assert inv is not None and inv.state is InvoiceState.DISPUTED

    assert approve(client, row.id).status_code == 200  # type: ignore[attr-defined]
    assert len(sender.sent) == 1


def test_paid_thanks_is_approvable_on_a_paid_invoice(session: Session) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    row = drafted(session, Intent.ALREADY_PAID)
    inv = session.get(Invoice, row.invoice_id)
    assert inv is not None and inv.status is InvoiceStatus.PAID
    assert row.kind == "reply_paid_thanks"

    assert approve(client, row.id).status_code == 200  # type: ignore[attr-defined]
    assert len(sender.sent) == 1


def test_paid_thanks_is_rejected_if_the_invoice_is_no_longer_paid(session: Session) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    row = drafted(session, Intent.ALREADY_PAID)
    inv = session.get(Invoice, row.invoice_id)
    assert inv is not None
    inv.status = InvoiceStatus.VOID
    session.flush()

    resp = approve(client, row.id)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    assert sender.sent == []
    session.refresh(row)
    assert row.status is OutboundStatus.REJECTED


@pytest.mark.parametrize("status", [InvoiceStatus.VOID, InvoiceStatus.PAID])
def test_holding_reply_is_rejected_when_the_invoice_is_void_or_paid(
    session: Session, status: InvoiceStatus
) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    row = drafted(session, Intent.DISPUTE)
    inv = session.get(Invoice, row.invoice_id)
    assert inv is not None
    inv.status = status
    session.flush()

    resp = approve(client, row.id)

    assert resp.status_code == 409  # type: ignore[attr-defined]
    assert resp.json() == {"error": "invoice_not_open"}  # type: ignore[attr-defined]
    assert sender.sent == []
    session.refresh(row)
    assert (row.status, row.rejected_by) == (OutboundStatus.REJECTED, "system")


def test_remittance_request_is_rejected_once_the_invoice_has_been_paid(session: Session) -> None:
    sender = FakeEmailSender()
    client = make_client(session=session, email_sender=sender)
    inv = open_invoice(session)
    msg = make_message(session, inv)
    run_route(
        session, inv, msg, make_classification(Intent.ALREADY_PAID), gateway=FakeStripeGateway()
    )
    [row] = drafts(session)
    assert row.kind == "reply_remittance_request"
    inv.status = InvoiceStatus.PAID  # the Stripe webhook got there first
    session.flush()

    assert approve(client, row.id).status_code == 409  # type: ignore[attr-defined]
    assert sender.sent == []


def test_a_stuck_reply_can_be_resolved_as_sent(session: Session) -> None:
    client = make_client(session=session, email_sender=FakeEmailSender())
    row = drafted(session, Intent.DISPUTE)
    row.status = OutboundStatus.SENDING
    session.flush()
    resp = client.post(
        f"/outbox/{row.id}/resolve",
        json={"resolved_by": "sam", "outcome": "sent", "provider_message_id": "pm-1"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert session.scalar(select(func.count()).select_from(Reminder)) == 0


def test_outbox_view_exposes_threading_and_the_inbound_message(session: Session) -> None:
    client = make_client(session=session)
    row = drafted(session, Intent.DISPUTE)
    body = client.get(f"/outbox/{row.id}", headers=AUTH).json()
    assert body["kind"] == "reply_holding"
    assert body["in_reply_to_message_id"] == "<reply-1@mail.acme.test>"
    assert body["references"] == "<earlier@x> <reply-1@mail.acme.test>"
    assert body["inbound_message_id"] == str(row.inbound_message_id)


def test_end_to_end_webhook_then_approve_threads_on_the_real_message_id(session: Session) -> None:
    sender = FakeEmailSender()
    inv = open_invoice(session)
    client = make_client(
        session=session,
        email_sender=sender,
        classifier=FakeClassifier(make_classification(Intent.DISPUTE)),
    )
    body = payload(
        message_id="<abc@mail.acme.test>",
        mailbox_hash=inv.reply_token,
        extra_headers=headers(References="<one@x>"),
    )
    assert client.post("/webhooks/inbound", json=body, headers=BASIC).status_code == 200
    [row] = drafts(session)
    assert approve(client, row.id).status_code == 200  # type: ignore[attr-defined]
    assert sender.sent[0].headers == (
        ("In-Reply-To", "<abc@mail.acme.test>"),
        ("References", "<one@x> <abc@mail.acme.test>"),
    )
