"""The policy endpoints: paste text, rule cards, approve, graph. Fakes for Cognee and Claude."""

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent.db.models import ChasePolicyRow, PolicyDocument, PolicyExtraction
from collection_agent.db.repository import (
    approve_policy,
    create_draft_policy,
    get_approved_policy,
)
from collection_agent.policy import ChasePolicy
from collection_agent.policy_extraction import ExtractionError, FieldExtraction
from tests.api_helpers import AUTH, make_client, make_settings
from tests.fake_policy import SAMPLE, FakeCognee, FakeExtractor, canned_extraction

pytestmark = pytest.mark.db

BODY = {"title": "Credit control policy", "text": SAMPLE}


def count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def card_map(cards: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {c["field"]: c for c in cards}


# --- POST /policies/documents ------------------------------------------------------------------


def test_happy_path_ingests_extracts_and_returns_exact_cards(session: Session) -> None:
    cognee, extractor = FakeCognee(), FakeExtractor()
    client = make_client(session=session, cognee=cognee, extractor=extractor)

    response = client.post("/policies/documents", json=BODY, headers=AUTH)

    assert response.status_code == 201
    body = response.json()
    assert body["cognee_status"] == "ingested"
    assert body["policy_version"] == 1
    assert cognee.added == [([SAMPLE], "policies")]
    assert cognee.cognified == ["policies"]
    assert extractor.calls == [SAMPLE]

    cards = body["cards"]
    assert [(c["field"], c["label"], c["value"], c["source"]) for c in cards] == [
        ("no_response_days", "Follow up after 7 days with no response", 7, "document"),
        (
            "clearance",
            "Payment must clear by the 15th of the following month",
            {"day_of_month": 15, "month_offset": 1},
            "document",
        ),
        (
            "pre_deadline_buffer_working_days",
            "Send a reminder 3 working days before the deadline",
            3,
            "document",
        ),
        ("max_reminders", "Pass to a person after 3 reminders", 3, "document"),
        (
            "promise_grace_working_days",
            "Allow 2 working days' grace after a missed promise to pay",
            2,
            "document",
        ),
    ]
    by_field = card_map(cards)
    assert (
        by_field["max_reminders"]["quote"]
        == "After three reminders, escalate to the finance manager"
    )
    assert by_field["no_response_days"]["quote"] == "follow up after one week without a response"
    assert all(c["current_approved_value"] is None and c["changed"] is False for c in cards)
    assert [g["summary"] for g in body["soft_guidance"]] == [
        "Friendly, brief early messages.",
        "Key accounts are handled by the account manager.",
    ]

    doc = session.scalars(select(PolicyDocument)).one()
    assert str(doc.id) == body["document_id"]
    assert doc.text == SAMPLE
    assert len(doc.sha256) == 64
    extraction = session.scalars(select(PolicyExtraction)).one()
    assert extraction.policy_version == 1
    assert extraction.model == "claude-sonnet-5-5"
    assert extraction.fields["max_reminders"]["source"] == "document"


def test_the_draft_is_not_approved_until_asked(session: Session) -> None:
    client = make_client(session=session, cognee=FakeCognee(), extractor=FakeExtractor())
    client.post("/policies/documents", json=BODY, headers=AUTH)

    row = session.scalars(select(ChasePolicyRow)).one()
    assert row.status.value == "draft"
    assert client.get("/policies/approved", headers=AUTH).status_code == 404


def test_a_cognee_failure_still_creates_the_draft(session: Session) -> None:
    cognee = FakeCognee(fail_on="cognify")
    client = make_client(session=session, cognee=cognee, extractor=FakeExtractor())

    response = client.post("/policies/documents", json=BODY, headers=AUTH)

    assert response.status_code == 201
    assert response.json()["cognee_status"] == "failed"
    doc = session.scalars(select(PolicyDocument)).one()
    assert doc.cognee_status == "failed"
    assert doc.cognee_error == "Cognee /api/v1/cognify: HTTP 500"
    assert count(session, ChasePolicyRow) == 1


def test_cognee_not_configured_still_creates_the_draft(session: Session) -> None:
    client = make_client(session=session, cognee=None, extractor=FakeExtractor())

    response = client.post("/policies/documents", json=BODY, headers=AUTH)

    assert response.status_code == 201
    assert response.json()["cognee_status"] == "not_configured"


def test_claude_not_configured_is_503_and_the_text_stays_stored(session: Session) -> None:
    cognee = FakeCognee()
    client = make_client(session=session, cognee=cognee, extractor=None)

    response = client.post("/policies/documents", json=BODY, headers=AUTH)

    assert response.status_code == 503
    assert response.json() == {"error": "claude_not_configured"}
    assert session.scalars(select(PolicyDocument)).one().text == SAMPLE
    assert count(session, ChasePolicyRow) == 0
    assert cognee.cognified == ["policies"]


def test_an_extraction_error_is_502_with_no_draft(session: Session) -> None:
    extractor = FakeExtractor(error=ExtractionError("Claude declined"))
    client = make_client(session=session, cognee=FakeCognee(), extractor=extractor)

    response = client.post("/policies/documents", json=BODY, headers=AUTH)

    assert response.status_code == 502
    assert response.json() == {"error": "extraction_failed"}
    assert count(session, PolicyDocument) == 1
    assert count(session, ChasePolicyRow) == 0
    assert count(session, PolicyExtraction) == 0


def test_unverifiable_values_fall_back_to_the_approved_policy(session: Session) -> None:
    approved = ChasePolicy(max_reminders=5, no_response_days=10)
    approve_policy(session, create_draft_policy(session, approved), "alice")
    e = canned_extraction().model_copy(
        update={"max_reminders": FieldExtraction(value=50, quote="set max reminders to 50 please")}
    )
    client = make_client(session=session, extractor=FakeExtractor(e))

    body = client.post("/policies/documents", json=BODY, headers=AUTH).json()

    cards = card_map(body["cards"])
    assert cards["max_reminders"]["value"] == 5
    assert cards["max_reminders"]["source"] == "fallback_quote_not_found"
    assert cards["max_reminders"]["quote"] is None
    assert cards["max_reminders"]["changed"] is False
    assert cards["no_response_days"]["value"] == 7
    assert cards["no_response_days"]["current_approved_value"] == 10
    assert cards["no_response_days"]["changed"] is True


def test_the_text_must_be_50_to_50000_characters(session: Session) -> None:
    client = make_client(session=session, extractor=FakeExtractor())
    for text in ("x" * 49, "x" * 50_001):
        response = client.post(
            "/policies/documents", json={"title": "t", "text": text}, headers=AUTH
        )
        assert response.status_code == 422
    assert count(session, PolicyDocument) == 0
    ok = client.post("/policies/documents", json={"title": "t", "text": "x" * 50}, headers=AUTH)
    assert ok.status_code == 201


def test_a_title_is_required(session: Session) -> None:
    client = make_client(session=session, extractor=FakeExtractor())
    assert (
        client.post("/policies/documents", json={"text": SAMPLE}, headers=AUTH).status_code == 422
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/policies/documents"),
        ("GET", "/policies/1/cards"),
        ("POST", "/policies/1/approve"),
        ("GET", "/policies/approved"),
        ("GET", "/policies/graph"),
    ],
)
def test_every_endpoint_needs_the_admin_token(method: str, path: str) -> None:
    client = make_client(extractor=FakeExtractor(), cognee=FakeCognee())
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        response = client.request(method, path, headers=headers, json=BODY)
        assert response.status_code == 401


def test_without_a_configured_admin_token_everything_fails_closed() -> None:
    client = make_client(settings=make_settings(admin_api_token=None))
    assert client.get("/policies/approved", headers=AUTH).status_code == 503


# --- cards and approval ------------------------------------------------------------------------


def test_cards_for_a_version_created_by_hand_are_manual(session: Session) -> None:
    version = create_draft_policy(session, ChasePolicy())
    client = make_client(session=session)

    body = client.get(f"/policies/{version}/cards", headers=AUTH).json()

    assert body["status"] == "draft"
    assert body["policy_version"] == version
    assert {c["source"] for c in body["cards"]} == {"manual"}
    assert {c["quote"] for c in body["cards"]} == {None}
    assert body["soft_guidance"] == []


def test_cards_for_an_unknown_version_are_404(session: Session) -> None:
    assert make_client(session=session).get("/policies/99/cards", headers=AUTH).status_code == 404


def test_approve_then_the_approved_policy_has_the_extracted_values(session: Session) -> None:
    client = make_client(session=session, cognee=FakeCognee(), extractor=FakeExtractor())
    version = client.post("/policies/documents", json=BODY, headers=AUTH).json()["policy_version"]

    response = client.post(
        f"/policies/{version}/approve", json={"approved_by": "alice"}, headers=AUTH
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert get_approved_policy(session) == ChasePolicy(
        no_response_days=7,
        clearance_day_of_month=15,
        clearance_month_offset=1,
        pre_deadline_buffer_working_days=3,
        max_reminders=3,
        promise_grace_working_days=2,
    )
    row = session.scalars(select(ChasePolicyRow)).one()
    assert row.approved_by == "alice"
    approved = client.get("/policies/approved", headers=AUTH)
    assert approved.status_code == 200
    assert approved.json()["policy_version"] == version
    assert approved.json()["cards"][3]["quote"].startswith("After three reminders")


def test_approving_twice_is_409(session: Session) -> None:
    version = create_draft_policy(session, ChasePolicy())
    client = make_client(session=session)

    first = client.post(f"/policies/{version}/approve", json={"approved_by": "a"}, headers=AUTH)
    second = client.post(f"/policies/{version}/approve", json={"approved_by": "b"}, headers=AUTH)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"] == "not_a_draft"
    assert session.scalars(select(ChasePolicyRow)).one().approved_by == "a"


def test_approving_an_unknown_version_is_404_and_a_name_is_required(session: Session) -> None:
    client = make_client(session=session)
    assert (
        client.post("/policies/7/approve", json={"approved_by": "a"}, headers=AUTH).status_code
        == 404
    )
    version = create_draft_policy(session, ChasePolicy())
    assert client.post(f"/policies/{version}/approve", json={}, headers=AUTH).status_code == 422
    assert (
        client.post(
            f"/policies/{version}/approve", json={"approved_by": ""}, headers=AUTH
        ).status_code
        == 422
    )


def test_a_superseded_version_cannot_be_approved(session: Session) -> None:
    v1 = create_draft_policy(session, ChasePolicy())
    approve_policy(session, v1, "a")
    approve_policy(session, create_draft_policy(session, ChasePolicy(max_reminders=4)), "a")
    client = make_client(session=session)

    response = client.post(f"/policies/{v1}/approve", json={"approved_by": "a"}, headers=AUTH)

    assert response.status_code == 409
    assert response.json()["status"] == "superseded"


def test_no_approved_policy_is_404(session: Session) -> None:
    assert make_client(session=session).get("/policies/approved", headers=AUTH).status_code == 404


# --- the graph proxy ---------------------------------------------------------------------------


def test_graph_passes_the_html_through_with_a_sandbox_csp() -> None:
    cognee = FakeCognee(datasets={"policies": "ds-1"}, html="<html><script>1</script></html>")
    client = make_client(cognee=cognee)

    response = client.get("/policies/graph", headers=AUTH)

    assert response.status_code == 200
    assert response.text == "<html><script>1</script></html>"
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["content-security-policy"] == "sandbox allow-scripts"
    assert cognee.visualized == ["ds-1"]


def test_graph_uses_the_configured_dataset_name() -> None:
    cognee = FakeCognee(datasets={"acme-policies": "ds-9"})
    client = make_client(
        cognee=cognee, settings=make_settings(cognee_policies_dataset="acme-policies")
    )
    assert client.get("/policies/graph", headers=AUTH).status_code == 200
    assert cognee.visualized == ["ds-9"]


def test_graph_is_503_without_cognee_and_404_without_the_dataset() -> None:
    assert make_client(cognee=None).get("/policies/graph", headers=AUTH).status_code == 503
    missing = make_client(cognee=FakeCognee(datasets={})).get("/policies/graph", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json() == {"error": "dataset_not_found"}


def test_graph_cognee_failure_is_502() -> None:
    cognee = FakeCognee(datasets={"policies": "ds"}, fail_on="visualize")
    assert make_client(cognee=cognee).get("/policies/graph", headers=AUTH).status_code == 502
