import json
import time
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Invoice, StripeEvent
from tests.api_helpers import make_client, make_settings
from tests.db_helpers import make_invoice
from tests.stripe_helpers import load_fixture, sign

URL = "/webhooks/stripe"
INVOICE_ID = "in_fixture_1"


def post(client: Any, payload: bytes, signature: str | None = None) -> Any:
    headers = {"Stripe-Signature": signature} if signature is not None else {}
    return client.post(URL, content=payload, headers=headers)


def signed_post(client: Any, name: str) -> Any:
    raw = json.dumps(load_fixture(name)).encode()
    return post(client, raw, sign(raw))


# --- signature and config: no database touched (a poison session proves it) -------------


def test_bad_signature_400_nothing_touched() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    resp = post(make_client(), raw, sign(raw, secret="whsec_some_other_fake"))
    assert resp.status_code == 400


def test_missing_signature_header_400() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    assert post(make_client(), raw).status_code == 400


def test_garbage_signature_header_400() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    assert post(make_client(), raw, "not-a-signature").status_code == 400


def test_tampered_body_400() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    signature = sign(raw)
    tampered = raw.replace(b"in_fixture_1", b"in_fixture_2")
    assert tampered != raw
    assert post(make_client(), tampered, signature).status_code == 400


def test_expired_timestamp_400() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    old = int(time.time()) - 3600
    assert post(make_client(), raw, sign(raw, timestamp=old)).status_code == 400


def test_validly_signed_non_event_400() -> None:
    raw = b'{"hello": "world"}'
    assert post(make_client(), raw, sign(raw)).status_code == 400


def test_missing_secret_503() -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    client = make_client(settings=make_settings(stripe_webhook_secret=None))
    assert post(client, raw, sign(raw)).status_code == 503


# --- database-backed ---------------------------------------------------------------------

db = pytest.mark.db


@pytest.fixture
def invoice(session: Session) -> Invoice:
    inv = make_invoice(session, stripe_invoice_id=INVOICE_ID)
    session.commit()  # the handler rolls back on failure; the invoice must survive that
    return inv


def event_count(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(StripeEvent)) or 0


@db
def test_bad_signature_stores_nothing(session: Session, invoice: Invoice) -> None:
    raw = json.dumps(load_fixture("invoice.paid")).encode()
    resp = post(make_client(session=session), raw, sign(raw, secret="whsec_wrong_fake"))
    assert resp.status_code == 400
    assert event_count(session) == 0
    session.refresh(invoice)
    assert invoice.status is InvoiceStatus.OPEN


@db
def test_paid_event_marks_invoice_paid_and_records_event(
    session: Session, invoice: Invoice
) -> None:
    resp = signed_post(make_client(session=session), "invoice.paid")
    assert resp.status_code == 200
    session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
    event = session.scalars(select(StripeEvent)).one()
    assert (event.event_id, event.type, event.stripe_object_id, event.livemode) == (
        "evt_fixture_paid",
        "invoice.paid",
        INVOICE_ID,
        False,
    )


@db
def test_duplicate_event_is_a_noop_200(session: Session, invoice: Invoice) -> None:
    client = make_client(session=session)
    assert signed_post(client, "invoice.paid").status_code == 200
    # Reopen the invoice by hand: a replayed event must not touch it again.
    invoice.status = InvoiceStatus.OPEN
    session.commit()
    again = signed_post(client, "invoice.paid")
    assert again.status_code == 200
    session.refresh(invoice)
    assert invoice.status is InvoiceStatus.OPEN
    assert event_count(session) == 1


@db
def test_voided_event_marks_void(session: Session, invoice: Invoice) -> None:
    assert signed_post(make_client(session=session), "invoice.voided").status_code == 200
    session.refresh(invoice)
    assert invoice.status is InvoiceStatus.VOID


@db
def test_marked_uncollectible_moves_to_with_human(session: Session, invoice: Invoice) -> None:
    resp = signed_post(make_client(session=session), "invoice.marked_uncollectible")
    assert resp.status_code == 200
    session.refresh(invoice)
    assert invoice.state is InvoiceState.WITH_HUMAN
    assert invoice.status is InvoiceStatus.OPEN


@db
@pytest.mark.parametrize("name", ["invoice.payment_failed", "invoice.overdue", "customer.created"])
def test_recorded_only_events_leave_invoice_unchanged(
    session: Session, invoice: Invoice, name: str
) -> None:
    resp = signed_post(make_client(session=session), name)
    assert resp.status_code == 200
    session.refresh(invoice)
    assert (invoice.status, invoice.state) == (InvoiceStatus.OPEN, InvoiceState.ACTIVE)
    assert event_count(session) == 1


@db
@pytest.mark.parametrize(
    ("first", "second", "final"),
    [
        ("invoice.paid", "invoice.voided", InvoiceStatus.PAID),
        ("invoice.voided", "invoice.paid", InvoiceStatus.VOID),
    ],
)
def test_terminal_status_never_moves_backwards(
    session: Session, invoice: Invoice, first: str, second: str, final: InvoiceStatus
) -> None:
    client = make_client(session=session)
    signed_post(client, first)
    assert signed_post(client, second).status_code == 200
    session.refresh(invoice)
    assert invoice.status is final
    assert event_count(session) == 2


@db
def test_unknown_invoice_is_recorded_and_200(session: Session) -> None:
    resp = signed_post(make_client(session=session), "invoice.paid")
    assert resp.status_code == 200
    assert event_count(session) == 1


@db
def test_processing_failure_rolls_back_event_and_returns_500(
    session: Session, invoice: Invoice, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collection_agent import webhooks_api

    def boom(*_: Any, **__: Any) -> None:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(webhooks_api, "_apply", boom)
    client = make_client(session=session)
    resp = signed_post(client, "invoice.paid")
    assert resp.status_code == 500
    assert event_count(session) == 0  # so Stripe's retry is not mistaken for a duplicate

    monkeypatch.undo()
    assert signed_post(client, "invoice.paid").status_code == 200
    session.refresh(invoice)
    assert invoice.status is InvoiceStatus.PAID
