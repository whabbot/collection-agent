"""The invoice list, the timeline, and the whole demo storyboard end to end with fakes."""

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.classifier import Intent
from collection_agent.db.models import Invoice, OutboundStatus
from tests.api_helpers import AUTH, make_client, make_settings
from tests.daily_helpers import approve_default_policy, chased_invoice
from tests.db_helpers import make_invoice
from tests.fake_classifier import FakeClassifier, absolute, make_classification
from tests.fake_policy import SAMPLE, FakeCognee, FakeExtractor
from tests.fake_stripe import FakeStripeGateway
from tests.inbound_helpers import sent_outbox
from tests.stripe_helpers import load_fixture, sign

pytestmark = pytest.mark.db

DEMO = make_settings(demo_mode=True, email_sender="log")


# --- GET /invoices ---------------------------------------------------------------------------


def test_invoice_list_needs_auth_and_keeps_the_escalated_filter(session: Session) -> None:
    client = make_client(session=session)
    assert client.get("/invoices").status_code == 401
    a = make_invoice(session, state=InvoiceState.ESCALATED)
    make_invoice(session)
    session.flush()
    rows = client.get("/invoices", params={"state": "escalated"}, headers=AUTH).json()
    assert [r["id"] for r in rows] == [str(a.id)]
    assert "reminder_count" in rows[0]  # the old shape is unchanged


def test_invoice_list_without_a_filter_is_newest_first(session: Session) -> None:
    chased_invoice(session, reference="A-1")
    second = chased_invoice(session, reference="A-2", status=InvoiceStatus.PAID)
    session.flush()
    rows = make_client(session=session).get("/invoices", headers=AUTH).json()
    assert [r["reference"] for r in rows] == ["A-2", "A-1"]
    row = next(r for r in rows if r["id"] == str(second.id))
    assert row["status"] == "paid" and row["state"] == "active"
    assert row["amount_minor"] == 12_000 and row["currency"] == "GBP"
    assert row["due_on"] == "2026-11-16"
    assert row["customer_name"] == "Acme Ltd" and row["customer_email"] == "ap@acme.test"
    assert row["hosted_invoice_url"] == "https://invoice.stripe.test/i/in_fake1"


def test_invalid_state_filter_is_rejected(session: Session) -> None:
    resp = make_client(session=session).get("/invoices", params={"state": "bogus"}, headers=AUTH)
    assert resp.status_code == 422


# --- GET /invoices/{id}/timeline -------------------------------------------------------------


def test_timeline_needs_auth_and_a_known_invoice(session: Session) -> None:
    client = make_client(session=session)
    url = "/invoices/6f0f6d0e-0000-4000-8000-000000000000/timeline"
    assert client.get(url).status_code == 401
    resp = client.get(url, headers=AUTH)
    assert resp.status_code == 404 and resp.json() == {"error": "invoice_not_found"}


def test_timeline_events_have_the_documented_shape(session: Session) -> None:
    inv = chased_invoice(session)
    events = make_client(session=session).get(f"/invoices/{inv.id}/timeline", headers=AUTH).json()
    assert [e["type"] for e in events] == ["invoice_created"]
    assert set(events[0]) == {"at", "type", "title", "detail", "tags"}
    assert events[0]["tags"] == []


def test_timeline_shows_escalation_with_its_rule_label(session: Session) -> None:
    inv = chased_invoice(session, state=InvoiceState.ESCALATED)
    inv.escalation_rule_label = "3 reminders sent without payment — passed to a person"
    inv.escalated_at = inv.created_at
    session.flush()
    events = make_client(session=session).get(f"/invoices/{inv.id}/timeline", headers=AUTH).json()
    escalated = next(e for e in events if e["type"] == "escalated")
    assert escalated["tags"] == ["3 reminders sent without payment — passed to a person"]


def test_timeline_shows_disputed_state(session: Session) -> None:
    inv = chased_invoice(session, state=InvoiceState.DISPUTED)
    events = make_client(session=session).get(f"/invoices/{inv.id}/timeline", headers=AUTH).json()
    assert "disputed" in [e["type"] for e in events]


# --- the storyboard ----------------------------------------------------------------------------


def stripe_paid(client: TestClient, stripe_invoice_id: str) -> Any:
    event = load_fixture("invoice.paid")
    event["id"] = "evt_demo_paid_1"
    event["data"]["object"]["id"] = stripe_invoice_id
    raw = json.dumps(event).encode()
    return client.post("/webhooks/stripe", content=raw, headers={"Stripe-Signature": sign(raw)})


def test_storyboard_from_policy_to_paid(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    gateway = FakeStripeGateway()
    classifier = FakeClassifier(
        make_classification(Intent.PROMISE_TO_PAY, 0.93, date_parts=absolute("october", 12))
    )
    client = make_client(
        session=session,
        settings=DEMO,
        gateway=gateway,
        classifier=classifier,
        cognee=FakeCognee(),
        extractor=FakeExtractor(),
        session_factory=session_factory,
        real_clock=True,
    )

    def ok(resp: Any, status: int = 200) -> Any:
        assert resp.status_code == status, resp.text
        return resp.json()

    # 0. the clock: an early-month weekday
    ok(client.post("/demo/api/clock", json={"today": "2026-10-01"}, headers=AUTH))

    # 1. policy text -> rule cards -> approve
    doc = ok(
        client.post("/policies/documents", json={"title": "Policy", "text": SAMPLE}, headers=AUTH),
        201,
    )
    ok(
        client.post(
            f"/policies/{doc['policy_version']}/approve",
            json={"approved_by": "Demo reviewer"},
            headers=AUTH,
        )
    )

    # 2. invoice + contract + invoice email, approved
    created = ok(
        client.post(
            "/invoices",
            json={
                "reference": "DEMO-1",
                "customer": {"name": "Acme Ltd", "email": "ap@example.com"},
                "currency": "gbp",
                "lines": [{"description": "Consulting", "amount_minor": 120000}],
            },
            headers=AUTH,
        ),
        201,
    )
    invoice_id = created["id"]
    ok(
        client.put(
            f"/invoices/{invoice_id}/contract",
            params={"filename": "contract.pdf"},
            content=b"%PDF-1.4 fake",
            headers=AUTH,
        )
    )
    drafted = ok(client.post(f"/invoices/{invoice_id}/invoice-email", headers=AUTH), 201)
    sent = ok(
        client.post(f"/outbox/{drafted['id']}/approve", json={"approved_by": "Demo"}, headers=AUTH)
    )
    assert sent["provider_message_id"].startswith("log-")

    # 3. skip 7 days -> the daily run drafts a tagged reminder -> approve
    ok(client.post("/demo/api/clock/advance", json={"days": 7}, headers=AUTH))
    report = ok(client.post("/demo/api/run-daily", headers=AUTH))
    assert report["run_date"] == "2026-10-08"
    assert report["counts"] == {"drafted:reminder[no_response]": 1}
    (reminder,) = ok(client.get("/outbox", headers=AUTH))
    assert "No response for 7 days" in reminder["rule_label"]
    ok(client.post(f"/outbox/{reminder['id']}/approve", json={"approved_by": "Demo"}, headers=AUTH))

    # 4. a simulated customer reply goes through the real inbound flow
    reply = ok(
        client.post(
            "/demo/api/simulate-reply",
            json={
                "invoice_id": invoice_id,
                "text": "Thanks for the reminder - we'll pay on the 12th.",
            },
            headers=AUTH,
        )
    )
    assert reply["route"] == "promise_accepted"
    (ack,) = ok(client.get("/outbox", headers=AUTH))
    assert ack["kind"] == "reply_promise_ack"
    ok(client.post(f"/outbox/{ack['id']}/approve", json={"approved_by": "Demo"}, headers=AUTH))

    # 5. Stripe says it was paid (a signed webhook; there is no shortcut)
    assert stripe_paid(client, created["stripe_invoice_id"]).status_code == 200

    events = ok(client.get(f"/invoices/{invoice_id}/timeline", headers=AUTH))
    types = [e["type"] for e in events]
    assert types == [
        "invoice_created",
        "contract_uploaded",
        "email_drafted",  # invoice email
        "email_approved",
        "email_sent",
        "email_drafted",  # reminder
        "email_approved",
        "email_sent",
        "reminder_recorded",
        "reply_received",
        "reply_classified",
        "reply_routed",
        "promise_recorded",
        "paused",
        "email_drafted",  # acknowledgement
        "email_approved",
        "email_sent",
        "stripe_event",
    ]
    assert [e["at"] for e in events] == sorted(e["at"] for e in events)
    reminder_events = [e for e in events if "No response for 7 days" in " ".join(e["tags"])]
    assert [e["type"] for e in reminder_events] == [
        "email_drafted",
        "email_approved",
        "email_sent",
        "reminder_recorded",
    ]
    classified = next(e for e in events if e["type"] == "reply_classified")
    assert classified["title"] == "Promise to pay (confidence 0.93)"
    assert next(e for e in events if e["type"] == "reply_routed")["detail"] == "promise_accepted"
    assert next(e for e in events if e["type"] == "promise_recorded")["title"].endswith(
        "12 October 2026"
    )
    assert events[-1]["title"] == "Paid in Stripe"
    assert events[-1]["tags"] == ["invoice.paid"]

    session.expire_all()
    invoice = session.get(Invoice, invoice_id)
    assert invoice is not None and invoice.status is InvoiceStatus.PAID
    assert invoice.promised_date == date(2026, 10, 12)
    listed = ok(client.get("/invoices", headers=AUTH))
    assert next(r for r in listed if r["id"] == invoice_id)["status"] == "paid"
    emails = ok(client.get("/demo/api/sent-emails", headers=AUTH))
    assert len(emails) == 3
    with_attachment = [e for e in emails if e["attachment_names"]]
    assert [e["attachment_names"] for e in with_attachment] == [["contract.pdf"]]  # invoice only


def test_timeline_for_a_rejected_email(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    row = sent_outbox(session, inv, "pm-provider-id-0001")
    row.status = OutboundStatus.REJECTED
    row.rejected_by, row.reject_reason = "Demo", "too soon"
    row.rejected_at = datetime.now(UTC)
    session.flush()
    events = make_client(session=session).get(f"/invoices/{inv.id}/timeline", headers=AUTH).json()
    assert [e["type"] for e in events] == ["invoice_created", "email_drafted", "email_rejected"]
    assert events[-1]["detail"] == "too soon (by Demo)"
