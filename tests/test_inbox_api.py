import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.orm import Session

from collection_agent.classifier import Intent
from collection_agent.db.models import InboundMessage
from tests.api_helpers import AUTH, NOW, make_client, make_settings
from tests.fake_classifier import FakeClassifier, make_classification
from tests.inbound_helpers import BASIC, payload
from tests.routing_helpers import make_message, open_invoice

# --- auth: no database ------------------------------------------------------------------------

ROUTES = [
    ("GET", "/inbox"),
    ("GET", f"/inbox/{uuid.uuid4()}"),
    ("POST", f"/inbox/{uuid.uuid4()}/review"),
]


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_inbox_needs_the_admin_token(method: str, path: str) -> None:
    client = make_client()
    assert client.request(method, path).status_code == 401
    bad = client.request(method, path, headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


@pytest.mark.parametrize(("method", "path"), ROUTES)
def test_inbox_fails_closed_without_a_configured_token(method: str, path: str) -> None:
    client = make_client(settings=make_settings(admin_api_token=None))
    assert client.request(method, path, headers=AUTH).status_code == 503


def test_the_inbound_credentials_do_not_open_the_inbox() -> None:
    assert make_client().get("/inbox", headers=BASIC).status_code == 401


# --- database -------------------------------------------------------------------------------------

db = pytest.mark.db


def add(session: Session, review: str, received: datetime = NOW, **kw: object) -> InboundMessage:
    inv = open_invoice(session)
    msg = make_message(session, inv, message_id=f"m-{uuid.uuid4()}@x", received_at=received)
    msg.review_status = review
    for k, v in kw.items():
        setattr(msg, k, v)
    session.flush()
    return msg


@db
def test_list_defaults_to_needs_review_newest_first(session: Session) -> None:
    old = add(session, "needs_review", NOW - timedelta(days=2))
    new = add(session, "needs_review", NOW)
    add(session, "not_needed")
    add(session, "reviewed")
    body = make_client(session=session).get("/inbox", headers=AUTH).json()
    assert [m["id"] for m in body] == [str(new.id), str(old.id)]


@db
@pytest.mark.parametrize("status", ["not_needed", "reviewed", "needs_review"])
def test_list_filters_by_review_status(session: Session, status: str) -> None:
    wanted = add(session, status)
    for other in {"not_needed", "reviewed", "needs_review"} - {status}:
        add(session, other)
    body = make_client(session=session).get(f"/inbox?review_status={status}", headers=AUTH).json()
    assert [m["id"] for m in body] == [str(wanted.id)]


@db
def test_list_rejects_an_unknown_status(session: Session) -> None:
    assert (
        make_client(session=session).get("/inbox?review_status=x", headers=AUTH).status_code == 422
    )


@db
def test_list_item_shape_and_truncated_text(session: Session) -> None:
    msg = add(
        session,
        "needs_review",
        classified_text="y" * 900,
        intent="dispute",
        intent_confidence=0.7,
        route="dispute",
        match_method="reply_token",
    )
    [item] = make_client(session=session).get("/inbox", headers=AUTH).json()
    assert item["id"] == str(msg.id)
    assert item["from_address"] == "ap@acme.test"
    assert item["subject"] == "Invoice INV-1"
    assert item["classified_text"] == "y" * 500
    assert (item["intent"], item["intent_confidence"], item["route"]) == ("dispute", 0.7, "dispute")
    assert item["match_method"] == "reply_token"
    assert item["invoice_reference"].startswith("REF-")
    assert item["drafted_outbox_ids"] == []
    assert "body_text" not in item


@db
def test_list_includes_the_drafted_outbox_id(session: Session) -> None:
    inv = open_invoice(session)
    client = make_client(
        session=session, classifier=FakeClassifier(make_classification(Intent.DISPUTE))
    )
    client.post("/webhooks/inbound", json=payload(mailbox_hash=inv.reply_token), headers=BASIC)
    [item] = client.get("/inbox", headers=AUTH).json()
    [outbox] = client.get("/outbox", headers=AUTH).json()
    assert item["drafted_outbox_ids"] == [outbox["id"]]


@db
def test_detail_has_the_full_body_and_classification(session: Session) -> None:
    msg = add(
        session,
        "needs_review",
        body_text="full body " * 100,
        intent="promise_to_pay",
        intent_probabilities={"promise_to_pay": 0.8, "other": 0.2},
        classifier_model="jev-1.13.0",
        promised_date_raw={"date_form": "relative"},
        attachment_count=2,
    )
    body = make_client(session=session).get(f"/inbox/{msg.id}", headers=AUTH).json()
    assert body["body_text"] == "full body " * 100
    assert body["intent_probabilities"] == {"promise_to_pay": 0.8, "other": 0.2}
    assert body["classifier_model"] == "jev-1.13.0"
    assert body["promised_date_raw"] == {"date_form": "relative"}
    assert body["attachment_count"] == 2
    assert body["reviewed_by"] is None


@db
def test_detail_404(session: Session) -> None:
    resp = make_client(session=session).get(f"/inbox/{uuid.uuid4()}", headers=AUTH)
    assert resp.status_code == 404


@db
def test_review_logs_what_the_person_did_and_changes_no_invoice(session: Session) -> None:
    msg = add(session, "needs_review", intent="dispute")
    inv = open_invoice(session)  # unrelated, to prove nothing else moves
    before = (inv.status, inv.state, inv.promised_date, inv.paused_until)
    client = make_client(session=session)

    resp = client.post(
        f"/inbox/{msg.id}/review",
        json={
            "reviewed_by": " sam ",
            "reviewer_intent": "request_extension",
            "note": " rang them ",
        },
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["review_status"] == "reviewed"
    assert body["reviewed_by"] == "sam"
    assert body["reviewer_intent"] == "request_extension"
    assert body["review_note"] == "rang them"
    assert body["intent"] == "dispute"  # the classification stays next to the human's call
    session.refresh(msg)
    assert msg.reviewed_at == NOW
    assert (inv.status, inv.state, inv.promised_date, inv.paused_until) == before
    assert client.get("/inbox", headers=AUTH).json() == []


@db
def test_review_without_intent_or_note(session: Session) -> None:
    msg = add(session, "needs_review")
    resp = make_client(session=session).post(
        f"/inbox/{msg.id}/review", json={"reviewed_by": "sam"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["reviewer_intent"] is None


@db
def test_review_twice_is_a_conflict(session: Session) -> None:
    msg = add(session, "needs_review")
    client = make_client(session=session)
    first = client.post(f"/inbox/{msg.id}/review", json={"reviewed_by": "sam"}, headers=AUTH)
    second = client.post(f"/inbox/{msg.id}/review", json={"reviewed_by": "jo"}, headers=AUTH)
    assert (first.status_code, second.status_code) == (200, 409)
    session.refresh(msg)
    assert msg.reviewed_by == "sam"


@db
def test_review_validation(session: Session) -> None:
    msg = add(session, "needs_review")
    client = make_client(session=session)
    url = f"/inbox/{msg.id}/review"
    assert client.post(url, json={}, headers=AUTH).status_code == 422
    assert client.post(url, json={"reviewed_by": " "}, headers=AUTH).status_code == 422
    bad_intent = {"reviewed_by": "sam", "reviewer_intent": "refund"}
    assert client.post(url, json=bad_intent, headers=AUTH).status_code == 422
    assert client.post(url, json={"reviewed_by": "s", "x": 1}, headers=AUTH).status_code == 422
    missing = f"/inbox/{uuid.uuid4()}/review"
    assert client.post(missing, json={"reviewed_by": "s"}, headers=AUTH).status_code == 404


@db
def test_review_can_link_an_unmatched_message_to_an_invoice_without_rerouting(
    session: Session,
) -> None:
    inv = open_invoice(session)
    client = make_client(session=session, classifier=FakeClassifier())
    client.post("/webhooks/inbound", json=payload(from_email="x@else.test"), headers=BASIC)
    [item] = client.get("/inbox", headers=AUTH).json()
    assert item["invoice_id"] is None

    resp = client.post(
        f"/inbox/{item['id']}/review",
        json={"reviewed_by": "sam", "invoice_id": str(inv.id)},
        headers=AUTH,
    )

    assert resp.status_code == 200
    assert resp.json()["invoice_id"] == str(inv.id)
    assert resp.json()["route"] == "unmatched"  # not re-routed
    assert resp.json()["match_method"] == "none"
    session.refresh(inv)
    assert inv.state.value == "active"
    assert client.get("/outbox", headers=AUTH).json() == []


@db
def test_linking_an_already_matched_message_or_a_missing_invoice_is_refused(
    session: Session,
) -> None:
    matched = add(session, "needs_review")
    client = make_client(session=session)
    other = open_invoice(session)
    session.commit()  # the refusals below roll back, which would otherwise discard the setup
    resp = client.post(
        f"/inbox/{matched.id}/review",
        json={"reviewed_by": "sam", "invoice_id": str(other.id)},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert resp.json() == {"error": "already_matched"}

    unmatched = add(session, "needs_review")
    unmatched.invoice_id = None
    session.commit()
    resp = client.post(
        f"/inbox/{unmatched.id}/review",
        json={"reviewed_by": "sam", "invoice_id": str(uuid.uuid4())},
        headers=AUTH,
    )
    assert resp.status_code == 404
    session.refresh(unmatched)
    assert unmatched.review_status == "needs_review"


def test_utc_is_used_for_review_time() -> None:
    assert NOW.tzinfo is UTC
