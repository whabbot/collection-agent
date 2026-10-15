import hashlib
import uuid
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Contract, Invoice, OutboundEmail, OutboundStatus
from collection_agent.db.repository import list_open_invoice_ids
from collection_agent.email_sender import EmailSendError
from tests.api_helpers import AUTH, make_client, make_settings
from tests.db_helpers import make_invoice
from tests.fake_email import FakeEmailSender

PDF = b"%PDF-1.7\nfake contract bytes\n%%EOF"
URL = "https://invoice.stripe.test/i/in_fake1"
TODAY = date(2026, 10, 15)
db = pytest.mark.db


def open_invoice(session: Session, **kw: object) -> Invoice:
    kw.setdefault("sent_on", None)
    inv = make_invoice(session, **kw)  # type: ignore[arg-type]
    inv.hosted_invoice_url = URL
    inv.contract_id = "CTR-17"
    session.flush()
    return inv


def setup(session: Session) -> tuple[TestClient, FakeEmailSender]:
    sender = FakeEmailSender()
    return make_client(session=session, email_sender=sender, today=TODAY), sender


def upload(client: TestClient, inv: Invoice, content: bytes = PDF, filename: str = "c.pdf") -> int:
    return client.put(
        f"/invoices/{inv.id}/contract",
        params={"filename": filename},
        content=content,
        headers={**AUTH, "Content-Type": "application/pdf"},
    ).status_code


def draft(client: TestClient, inv: Invoice) -> httpx.Response:
    return client.post(f"/invoices/{inv.id}/invoice-email", headers=AUTH)


def approve(client: TestClient, outbox_id: str) -> httpx.Response:
    return client.post(f"/outbox/{outbox_id}/approve", json={"approved_by": "sam"}, headers=AUTH)


def ready(session: Session) -> tuple[TestClient, FakeEmailSender, Invoice, str]:
    client, sender = setup(session)
    inv = open_invoice(session)
    assert upload(client, inv) == 200
    resp = draft(client, inv)
    assert resp.status_code == 201
    return client, sender, inv, resp.json()["id"]


# --- auth / config: no database -----------------------------

ROUTES = [
    ("PUT", "/invoices/{id}/contract?filename=c.pdf"),
    ("POST", "/invoices/{id}/invoice-email"),
    ("GET", "/outbox"),
    ("GET", "/outbox/{id}"),
    ("POST", "/outbox/{id}/approve"),
    ("POST", "/outbox/{id}/reject"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_401_and_503_for_auth(method: str, path: str) -> None:
    path = path.format(id=uuid.uuid4())
    assert make_client().request(method, path).status_code == 401
    no_token = make_client(settings=make_settings(admin_api_token=None))
    assert no_token.request(method, path).status_code == 503


def test_draft_503_when_email_settings_missing() -> None:
    client = make_client(settings=make_settings(company_name=None))
    resp = client.post(f"/invoices/{uuid.uuid4()}/invoice-email", headers=AUTH)
    assert resp.status_code == 503


def test_approve_503_without_postmark_token_touches_nothing() -> None:
    client = make_client(settings=make_settings(postmark_server_token=None))
    resp = client.post(f"/outbox/{uuid.uuid4()}/approve", json={"approved_by": "sam"}, headers=AUTH)
    assert resp.status_code == 503  # PoisonSession would fail the test if the DB were touched


@pytest.mark.parametrize(
    "filename", ["", "x.txt", "../x.pdf", "a/b.pdf", "a\\b.pdf", ".pdf", "x" * 120 + ".pdf"]
)
def test_bad_filename_422(filename: str) -> None:
    client = make_client()
    resp = client.put(
        f"/invoices/{uuid.uuid4()}/contract",
        params={"filename": filename},
        content=PDF,
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_missing_filename_422() -> None:
    resp = make_client().put(f"/invoices/{uuid.uuid4()}/contract", content=PDF, headers=AUTH)
    assert resp.status_code == 422


@pytest.mark.parametrize("content", [b"", b"not a pdf", b"x" * (7 * 1024 * 1024 + 1)])
def test_bad_body_422_before_any_db_access(content: bytes) -> None:
    resp = make_client().put(
        f"/invoices/{uuid.uuid4()}/contract",
        params={"filename": "c.pdf"},
        content=content,
        headers=AUTH,
    )
    assert resp.status_code == 422


def test_oversize_pdf_422_even_with_valid_magic() -> None:
    big = b"%PDF-" + b"x" * (7 * 1024 * 1024)
    resp = make_client().put(
        f"/invoices/{uuid.uuid4()}/contract",
        params={"filename": "c.pdf"},
        content=big,
        headers=AUTH,
    )
    assert resp.status_code == 422


# --- contract upload -----------------------------


@db
def test_upload_happy_path_and_replace(session: Session) -> None:
    client, _ = setup(session)
    inv = open_invoice(session)
    resp = client.put(
        f"/invoices/{inv.id}/contract",
        params={"filename": "My Contract (v1).pdf"},
        content=PDF,
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "filename": "My Contract (v1).pdf",
        "size_bytes": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }
    newer = PDF + b"v2"
    assert upload(client, inv, newer, "v2.pdf") == 200
    (row,) = session.scalars(select(Contract)).all()
    assert (row.filename, row.content, row.size_bytes) == ("v2.pdf", newer, len(newer))


@db
def test_upload_unknown_invoice_404(session: Session) -> None:
    client, _ = setup(session)
    resp = client.put(
        f"/invoices/{uuid.uuid4()}/contract",
        params={"filename": "c.pdf"},
        content=PDF,
        headers=AUTH,
    )
    assert resp.status_code == 404


@db
def test_upload_after_send_409(session: Session) -> None:
    client, _, inv, outbox_id = ready(session)
    assert approve(client, outbox_id).status_code == 200
    assert upload(client, inv, PDF + b"new") == 409


@db
def test_upload_while_sending_409(session: Session) -> None:
    client, _, inv, outbox_id = ready(session)
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    row.status = OutboundStatus.SENDING
    session.flush()
    assert upload(client, inv, PDF + b"new") == 409


# --- drafting -----------------------------


@db
def test_draft_happy_path(session: Session) -> None:
    client, _, inv, _ = ready(session)
    row = session.scalars(select(OutboundEmail)).one()
    assert row.status is OutboundStatus.PENDING_APPROVAL
    assert row.kind == "invoice"
    assert row.attach_contract is True
    assert row.to_address == "ap@acme.test"
    assert (row.from_address, row.from_name) == ("billing@billing.example.test", "Acme Billing")
    assert row.reply_to == f"reply+{inv.reply_token}@billing.example.test"
    assert row.subject == f"Invoice {inv.reference} from Acme Co"
    assert row.body_text == (
        "Hello Acme Ltd,\n\n"
        f"Please find attached your contract (CTR-17). Invoice {inv.reference} for £120.00 "
        "is due by Monday 16 November 2026.\n\n"
        "You can view and pay it securely here:\n"
        f"{URL}\n\n"
        "If you have any questions, just reply to this email.\n\n"
        "Thank you,\nAcme Billing\nAcme Co\n"
    )
    assert inv.sent_on is None


@db
def test_draft_unknown_invoice_404(session: Session) -> None:
    client, _ = setup(session)
    assert draft(client, Invoice(id=uuid.uuid4())).status_code == 404


@db
def test_draft_without_contract_409(session: Session) -> None:
    client, _ = setup(session)
    inv = open_invoice(session)
    resp = draft(client, inv)
    assert resp.status_code == 409
    assert resp.json() == {"error": "contract_missing"}


@db
@pytest.mark.parametrize(
    "change",
    [
        {"status": InvoiceStatus.PAID},
        {"status": InvoiceStatus.VOID},
        {"state": InvoiceState.DISPUTED},
        {"state": InvoiceState.WITH_HUMAN},
    ],
)
def test_draft_invoice_not_open_409(session: Session, change: dict[str, object]) -> None:
    client, _ = setup(session)
    inv = open_invoice(session, **change)
    assert upload(client, inv) == 200
    resp = draft(client, inv)
    assert resp.status_code == 409
    assert resp.json() == {"error": "invoice_not_open"}


@db
def test_second_draft_409_and_redraft_after_reject(session: Session) -> None:
    client, _, inv, outbox_id = ready(session)
    again = draft(client, inv)
    assert again.status_code == 409
    assert again.json() == {"error": "already_drafted"}

    rej = client.post(
        f"/outbox/{outbox_id}/reject", json={"rejected_by": "sam", "reason": "typo"}, headers=AUTH
    )
    assert rej.status_code == 200
    assert draft(client, inv).status_code == 201


@db
def test_draft_without_hosted_url_409(session: Session) -> None:
    client, _ = setup(session)
    inv = open_invoice(session)
    inv.hosted_invoice_url = None
    session.flush()
    assert upload(client, inv) == 200
    assert draft(client, inv).json() == {"error": "invoice_url_missing"}


# --- approve -----------------------------


@db
def test_approve_happy_path(session: Session) -> None:
    client, sender, inv, outbox_id = ready(session)
    assert inv.id not in list_open_invoice_ids(session)

    resp = approve(client, outbox_id)

    assert resp.status_code == 200
    out = resp.json()
    assert out["status"] == "sent"
    assert out["approved_by"] == "sam"
    assert out["provider_message_id"] == "fake-msg-1"
    (msg,) = sender.sent
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert (msg.from_name, msg.from_address) == ("Acme Billing", "billing@billing.example.test")
    assert msg.to == "ap@acme.test"
    assert msg.reply_to == row.reply_to
    assert (msg.subject, msg.text_body) == (row.subject, row.body_text)
    assert [(a.name, a.content, a.content_type) for a in msg.attachments] == [
        ("c.pdf", PDF, "application/pdf")
    ]
    assert msg.metadata == {"invoice_id": str(inv.id), "outbound_email_id": outbox_id}
    assert row.sent_at is not None
    session.refresh(inv)
    assert inv.sent_on == TODAY
    assert inv.id in list_open_invoice_ids(session)


@db
def test_approve_twice_sends_once(session: Session) -> None:
    client, sender, _, outbox_id = ready(session)
    assert approve(client, outbox_id).status_code == 200
    second = approve(client, outbox_id)
    assert second.status_code == 409
    assert second.json() == {"error": "not_pending"}
    assert len(sender.sent) == 1


@db
def test_approve_unknown_404(session: Session) -> None:
    client, _ = setup(session)
    assert approve(client, str(uuid.uuid4())).status_code == 404


@db
def test_approve_after_invoice_paid_rejects_and_does_not_send(session: Session) -> None:
    client, sender, inv, outbox_id = ready(session)
    inv.status = InvoiceStatus.PAID
    session.flush()

    resp = approve(client, outbox_id)

    assert resp.status_code == 409
    assert resp.json() == {"error": "invoice_not_open"}
    assert sender.sent == []
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert row.status is OutboundStatus.REJECTED
    assert row.reject_reason == "invoice no longer open"
    assert inv.sent_on is None


@db
def test_approve_send_failure_502_then_redraft(session: Session) -> None:
    client, sender, inv, outbox_id = ready(session)
    sender.fail_with = EmailSendError("Postmark rejected the send: ErrorCode 406", error_code=406)

    resp = approve(client, outbox_id)

    assert resp.status_code == 502
    assert resp.json() == {"error": "send_failed"}
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert row.status is OutboundStatus.FAILED
    assert row.error is not None
    assert inv.sent_on is None
    sender.fail_with = None
    assert draft(client, inv).status_code == 201


@db
def test_approve_unknown_outcome_stays_sending_and_blocks_redraft(session: Session) -> None:
    client, sender, inv, outbox_id = ready(session)
    sender.fail_with = EmailSendError("no usable reply", outcome_unknown=True)

    resp = approve(client, outbox_id)

    assert resp.status_code == 502
    assert resp.json() == {"error": "send_outcome_unknown"}
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert row.status is OutboundStatus.SENDING
    assert inv.sent_on is None
    assert draft(client, inv).status_code == 409
    assert approve(client, outbox_id).status_code == 409


@db
def test_approve_keeps_existing_sent_on(session: Session) -> None:
    client, _, inv, outbox_id = ready(session)
    inv.sent_on = date(2026, 10, 1)
    session.flush()
    assert approve(client, outbox_id).status_code == 200
    assert inv.sent_on == date(2026, 10, 1)


@db
def test_approve_without_postmark_token_leaves_row_pending(session: Session) -> None:
    _, _, _, outbox_id = ready(session)
    client = make_client(session=session, settings=make_settings(postmark_server_token=None))
    assert approve(client, outbox_id).status_code == 503
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert row.status is OutboundStatus.PENDING_APPROVAL


@db
@pytest.mark.parametrize("body", [{}, {"approved_by": ""}, {"approved_by": "x" * 101}])
def test_approve_body_validation_422(session: Session, body: dict[str, str]) -> None:
    client, sender, _, outbox_id = ready(session)
    resp = client.post(f"/outbox/{outbox_id}/approve", json=body, headers=AUTH)
    assert resp.status_code == 422
    assert sender.sent == []


# --- reject / listing -----------------------------


@db
def test_reject_happy_path(session: Session) -> None:
    client, sender, _, outbox_id = ready(session)
    resp = client.post(
        f"/outbox/{outbox_id}/reject",
        json={"rejected_by": "sam", "reason": "wrong amount"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    out = resp.json()
    assert (out["status"], out["rejected_by"], out["reject_reason"]) == (
        "rejected",
        "sam",
        "wrong amount",
    )
    assert out["rejected_at"] is not None
    assert sender.sent == []
    assert approve(client, outbox_id).status_code == 409


@db
def test_reject_sent_row_409(session: Session) -> None:
    client, _, _, outbox_id = ready(session)
    assert approve(client, outbox_id).status_code == 200
    resp = client.post(
        f"/outbox/{outbox_id}/reject", json={"rejected_by": "sam", "reason": "oops"}, headers=AUTH
    )
    assert resp.status_code == 409
    row = session.get(OutboundEmail, uuid.UUID(outbox_id))
    assert row is not None
    assert row.status is OutboundStatus.SENT


@db
def test_reject_unknown_404(session: Session) -> None:
    client, _ = setup(session)
    resp = client.post(
        f"/outbox/{uuid.uuid4()}/reject", json={"rejected_by": "s", "reason": "r"}, headers=AUTH
    )
    assert resp.status_code == 404


@db
def test_listing_filter_order_and_no_bytes(session: Session) -> None:
    client, _ = setup(session)
    ids = []
    for _ in range(2):
        inv = open_invoice(session)
        assert upload(client, inv) == 200
        ids.append(draft(client, inv).json()["id"])
    first = session.get(OutboundEmail, uuid.UUID(ids[0]))
    assert first is not None
    first.created_at = first.created_at.replace(year=2020)  # make ordering unambiguous
    session.flush()

    pending = client.get("/outbox", headers=AUTH)
    assert pending.status_code == 200
    assert [r["id"] for r in pending.json()] == [ids[1], ids[0]]
    assert "fake contract bytes" not in pending.text
    assert "content" not in pending.json()[0]
    assert pending.json()[0]["contract"] == {
        "filename": "c.pdf",
        "size_bytes": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }

    assert approve(client, ids[1]).status_code == 200
    assert [r["id"] for r in client.get("/outbox", headers=AUTH).json()] == [ids[0]]
    sent = client.get("/outbox", params={"status": "sent"}, headers=AUTH).json()
    assert [r["id"] for r in sent] == [ids[1]]
    assert client.get("/outbox", params={"status": "bogus"}, headers=AUTH).status_code == 422

    one = client.get(f"/outbox/{ids[0]}", headers=AUTH)
    assert one.status_code == 200
    assert one.json()["id"] == ids[0]
    assert client.get(f"/outbox/{uuid.uuid4()}", headers=AUTH).status_code == 404


@db
def test_unique_index_blocks_second_live_invoice_email(session: Session) -> None:
    _, _, inv, _ = ready(session)
    session.add(
        OutboundEmail(
            invoice_id=inv.id,
            kind="invoice",
            to_address="a@b.test",
            from_address="f@b.test",
            from_name="F",
            reply_to="r@b.test",
            subject="s",
            body_text="b",
            attach_contract=True,
            status=OutboundStatus.SENT,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
