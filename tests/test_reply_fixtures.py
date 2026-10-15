"""The synthetic reply set is well formed, and the injection cases cannot move an invoice."""

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceStatus
from collection_agent.classifier import Intent
from collection_agent.classifier_eval import DEFAULT_FIXTURES, EvalCase, load_cases
from tests.api_helpers import make_client
from tests.fake_classifier import FakeClassifier, make_classification
from tests.fake_stripe import FakeStripeGateway
from tests.inbound_helpers import BASIC, payload
from tests.routing_helpers import drafts, open_invoice

FIXTURES = Path(__file__).parent / "fixtures" / "replies.jsonl"
CASES = load_cases(FIXTURES)
INJECTIONS = [c for c in CASES if c.injection]


def test_default_fixture_path_points_at_this_file() -> None:
    assert (Path(__file__).parent.parent / DEFAULT_FIXTURES).resolve() == FIXTURES.resolve()


def test_at_least_thirty_replies_with_unique_ids() -> None:
    assert len(CASES) >= 30
    assert len({c.id for c in CASES}) == len(CASES)


def test_every_intent_is_covered() -> None:
    assert {c.expected_intent for c in CASES} == set(Intent)


def test_at_least_four_injection_cases() -> None:
    assert len(INJECTIONS) >= 4
    assert any("ignore previous instructions" in c.text.lower() for c in INJECTIONS)
    assert any("already agreed to waive" in c.text.lower() for c in INJECTIONS)
    assert any("already_paid" in c.text.lower() for c in INJECTIONS)


def test_dates_cover_absolute_and_relative_phrasing() -> None:
    dated = [c for c in CASES if c.expected_promised_date is not None]
    assert len(dated) >= 8
    assert any("end of the month" in c.text for c in dated)
    assert any(any(m in c.text for m in ("October", "November", "Nov", "January")) for c in dated)
    assert any("Friday" in c.text for c in dated)


def test_every_fixture_is_valid() -> None:
    for line in FIXTURES.read_text().splitlines():
        raw = json.loads(line)
        assert Intent(raw["expected_intent"])
        assert date.fromisoformat(raw["today"])
        assert isinstance(raw["text"], str) and raw["text"].strip()
        if raw.get("expected_promised_date"):
            assert date.fromisoformat(raw["expected_promised_date"]) >= date.fromisoformat(
                raw["today"]
            )
        if raw["expected_intent"] == "admin_request":
            assert raw["expected_admin_subtype"] in ("resend", "billing_change", "other")


def test_ambiguous_cases_are_present() -> None:
    notes = " ".join(
        str(json.loads(line).get("note", "")) for line in FIXTURES.read_text().splitlines()
    )
    assert "part payment" in notes
    assert "promise plus dispute" in notes


@pytest.mark.db
@pytest.mark.parametrize("case", INJECTIONS, ids=lambda c: c.id)
def test_even_if_the_classifier_is_fooled_an_injection_never_marks_an_invoice_paid(
    session: Session, case: EvalCase
) -> None:
    """Worst case: the model obeys the email and answers already_paid with high confidence."""
    inv = open_invoice(session)
    amount, due, currency = inv.amount_minor, inv.due_on, inv.currency
    gateway = FakeStripeGateway()  # Stripe says the invoice is still open
    client = make_client(
        session=session,
        gateway=gateway,
        classifier=FakeClassifier(make_classification(Intent.ALREADY_PAID, confidence=0.99)),
    )
    body = payload(text=case.text, mailbox_hash=inv.reply_token)
    assert client.post("/webhooks/inbound", json=body, headers=BASIC).status_code == 200
    session.refresh(inv)
    assert inv.status is InvoiceStatus.OPEN
    assert (inv.amount_minor, inv.due_on, inv.currency) == (amount, due, currency)
    assert [d.kind for d in drafts(session)] == ["reply_remittance_request"]
    assert case.text not in drafts(session)[0].body_text
