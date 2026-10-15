from datetime import UTC, date, datetime
from typing import Any

import pytest
import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent import invoices_api
from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Customer, Invoice
from collection_agent.db.repository import approve_policy, create_draft_policy
from collection_agent.invoices_api import due_timestamp
from collection_agent.policy import ChasePolicy
from tests.api_helpers import AUTH, make_client, make_settings
from tests.fake_stripe import FakeStripeGateway

BODY: dict[str, Any] = {
    "reference": "INV-2026-0042",
    "customer": {"name": "Acme Ltd", "email": "ap@acme.example"},
    "currency": "gbp",
    "lines": [
        {"description": "October retainer", "amount_minor": 250000},
        {"description": "Setup fee", "amount_minor": 5000},
    ],
    "contract_id": "CTR-17",
}


def with_(**changes: Any) -> dict[str, Any]:
    return {**BODY, **changes}


# --- no database needed: auth and validation -------------------------------------------


def test_401_without_token() -> None:
    client = make_client(gateway=FakeStripeGateway())
    assert client.post("/invoices", json=BODY).status_code == 401


def test_401_with_wrong_token() -> None:
    client = make_client(gateway=FakeStripeGateway())
    resp = client.post("/invoices", json=BODY, headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_401_with_wrong_scheme() -> None:
    client = make_client(gateway=FakeStripeGateway())
    resp = client.post("/invoices", json=BODY, headers={"Authorization": "admin_test_fake"})
    assert resp.status_code == 401


def test_503_when_admin_token_not_configured() -> None:
    gateway = FakeStripeGateway()
    client = make_client(gateway=gateway, settings=make_settings(admin_api_token=None))
    resp = client.post("/invoices", json=BODY, headers=AUTH)
    assert resp.status_code == 503
    assert gateway.calls == []


def test_503_when_stripe_key_not_configured() -> None:
    # No gateway override, so the real dependency runs and must fail closed.
    client = make_client(settings=make_settings(stripe_secret_key=None))
    assert client.post("/invoices", json=BODY, headers=AUTH).status_code == 503


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "GBP"},
        {"currency": "gb"},
        {"lines": []},
        {"lines": [{"description": "x", "amount_minor": 0}]},
        {"lines": [{"description": "x", "amount_minor": -5}]},
        {"lines": [{"description": "", "amount_minor": 5}]},
        {"lines": [{"description": "x", "amount_minor": 99_999_999}] * 2},
        {"reference": ""},
        {"reference": "has space"},
        {"reference": "x" * 65},
        {"customer": {"name": "Acme", "email": "not-an-email"}},
        {"contract_id": "c" * 65},
    ],
)
def test_validation_422(changes: dict[str, Any]) -> None:
    gateway = FakeStripeGateway()
    resp = make_client(gateway=gateway).post("/invoices", json=with_(**changes), headers=AUTH)
    assert resp.status_code == 422
    assert gateway.calls == []


def test_too_many_lines_422() -> None:
    lines = [{"description": "x", "amount_minor": 1}] * 51
    resp = make_client(gateway=FakeStripeGateway()).post(
        "/invoices", json=with_(lines=lines), headers=AUTH
    )
    assert resp.status_code == 422


# --- due_date timestamps -----------------------------------------------------------------


def test_due_timestamp_is_end_of_day_london_bst() -> None:
    # 15 Sep 2026 is BST (UTC+1): 23:59:59 local is 22:59:59 UTC.
    ts = due_timestamp(date(2026, 9, 15))
    assert datetime.fromtimestamp(ts, tz=invoices_api.LONDON).isoformat() == (
        "2026-09-15T23:59:59+01:00"
    )
    assert ts == int(datetime(2026, 9, 15, 22, 59, 59, tzinfo=UTC).timestamp())


def test_due_timestamp_is_end_of_day_london_gmt() -> None:
    # 16 Nov 2026 is GMT (UTC+0).
    ts = due_timestamp(date(2026, 11, 16))
    assert datetime.fromtimestamp(ts, tz=invoices_api.LONDON).isoformat() == (
        "2026-11-16T23:59:59+00:00"
    )
    assert ts == int(datetime(2026, 11, 16, 23, 59, 59, tzinfo=UTC).timestamp())


# --- database-backed -----------------------------------------------------------------------

db = pytest.mark.db


@pytest.fixture
def approved(session: Session) -> None:
    version = create_draft_policy(session, ChasePolicy())
    approve_policy(session, version, "tester")
    session.commit()


def count(session: Session, model: type[Invoice] | type[Customer]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


@db
def test_happy_path(session: Session, approved: None) -> None:
    gateway = FakeStripeGateway()
    client = make_client(session=session, gateway=gateway, today=date(2026, 10, 15))

    resp = client.post("/invoices", json=BODY, headers=AUTH)

    assert resp.status_code == 201
    out = resp.json()
    assert out["reference"] == "INV-2026-0042"
    assert out["due_on"] == "2026-11-16"  # 15 Nov 2026 is a Sunday
    assert out["amount_minor"] == 255000
    assert out["currency"] == "gbp"
    assert out["status"] == "open"
    assert out["stripe_invoice_id"] == "in_fake2"
    assert out["hosted_invoice_url"] == "https://invoice.stripe.test/i/in_fake2"

    assert gateway.methods() == [
        "create_customer",
        "create_invoice",
        "add_invoice_item",
        "add_invoice_item",
        "finalize_invoice",
    ]
    keys = [params["idempotency_key"] for _, params in gateway.calls]
    assert keys == [
        "INV-2026-0042:customer",
        "INV-2026-0042:invoice",
        "INV-2026-0042:line:0",
        "INV-2026-0042:line:1",
        "INV-2026-0042:finalize",
    ]
    _, inv_call = gateway.calls[1]
    assert inv_call["metadata"] == {"reference": "INV-2026-0042", "contract_id": "CTR-17"}
    assert inv_call["due_date"] == due_timestamp(date(2026, 11, 16))
    _, item_call = gateway.calls[2]
    assert (item_call["amount_minor"], item_call["description"]) == (250000, "October retainer")

    inv = session.scalars(select(Invoice)).one()
    assert inv.sent_on is None
    assert inv.due_on == date(2026, 11, 16)
    assert inv.status is InvoiceStatus.OPEN
    assert inv.state is InvoiceState.ACTIVE
    assert inv.amount_minor == 255000
    assert inv.contract_id == "CTR-17"
    assert inv.hosted_invoice_url == out["hosted_invoice_url"]
    assert session.scalars(select(Customer)).one().stripe_customer_id == "cus_fake1"


@db
def test_contract_id_omitted_from_metadata_when_null(session: Session, approved: None) -> None:
    gateway = FakeStripeGateway()
    body = {k: v for k, v in BODY.items() if k != "contract_id"}
    resp = make_client(session=session, gateway=gateway).post("/invoices", json=body, headers=AUTH)
    assert resp.status_code == 201
    assert gateway.calls[1][1]["metadata"] == {"reference": "INV-2026-0042"}


@db
def test_due_on_rolls_over_year_end(session: Session, approved: None) -> None:
    client = make_client(session=session, gateway=FakeStripeGateway(), today=date(2026, 12, 20))
    resp = client.post("/invoices", json=BODY, headers=AUTH)
    assert resp.json()["due_on"] == "2027-01-15"  # a Friday


@db
def test_reuses_existing_customer_by_email_case_insensitively(
    session: Session, approved: None
) -> None:
    gateway = FakeStripeGateway()
    client = make_client(session=session, gateway=gateway)
    assert client.post("/invoices", json=BODY, headers=AUTH).status_code == 201

    second = with_(
        reference="INV-2026-0043", customer={"name": "Acme Ltd", "email": "AP@Acme.Example"}
    )
    assert client.post("/invoices", json=second, headers=AUTH).status_code == 201

    assert gateway.count_created("create_customer") == 1
    assert count(session, Customer) == 1
    assert count(session, Invoice) == 2
    second_invoice_call = [p for m, p in gateway.calls if m == "create_invoice"][1]
    assert second_invoice_call["customer_id"] == "cus_fake1"


@db
def test_same_reference_same_payload_returns_200_without_stripe(
    session: Session, approved: None
) -> None:
    gateway = FakeStripeGateway()
    client = make_client(session=session, gateway=gateway)
    first = client.post("/invoices", json=BODY, headers=AUTH)
    n_calls = len(gateway.calls)

    again = client.post(
        "/invoices",
        json=with_(customer={"name": "Acme Ltd", "email": "AP@ACME.EXAMPLE"}),
        headers=AUTH,
    )

    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(gateway.calls) == n_calls
    assert count(session, Invoice) == 1


@pytest.mark.db
@pytest.mark.parametrize(
    "changes",
    [
        {"customer": {"name": "Other", "email": "other@acme.example"}},
        {"currency": "usd"},
        {"lines": [{"description": "October retainer", "amount_minor": 1}]},
    ],
)
def test_same_reference_different_payload_returns_409(
    session: Session, approved: None, changes: dict[str, Any]
) -> None:
    gateway = FakeStripeGateway()
    client = make_client(session=session, gateway=gateway)
    client.post("/invoices", json=BODY, headers=AUTH)
    n_calls = len(gateway.calls)

    resp = client.post("/invoices", json=with_(**changes), headers=AUTH)

    assert resp.status_code == 409
    assert resp.json() == {"error": "reference_conflict"}
    assert len(gateway.calls) == n_calls


@db
def test_no_approved_policy_returns_409(session: Session) -> None:
    gateway = FakeStripeGateway()
    resp = make_client(session=session, gateway=gateway).post("/invoices", json=BODY, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json() == {"error": "no_approved_policy"}
    assert gateway.calls == []


@db
@pytest.mark.parametrize(
    "method", ["create_customer", "create_invoice", "add_invoice_item", "finalize_invoice"]
)
def test_stripe_error_returns_502_and_stores_nothing(
    session: Session, approved: None, method: str
) -> None:
    gateway = FakeStripeGateway()
    gateway.fail_on[method] = stripe.APIConnectionError("boom")
    resp = make_client(session=session, gateway=gateway).post("/invoices", json=BODY, headers=AUTH)
    assert resp.status_code == 502
    assert resp.json() == {"error": "stripe_error"}
    assert count(session, Invoice) == 0
    assert count(session, Customer) == 0


@db
def test_stripe_error_log_has_request_id_but_not_the_key(
    session: Session, approved: None, caplog: pytest.LogCaptureFixture
) -> None:
    gateway = FakeStripeGateway()
    err = stripe.AuthenticationError(
        "Invalid API Key provided: sk_test_fake_not_real", headers={"request-id": "req_fake123"}
    )
    gateway.fail_on["create_customer"] = err
    make_client(session=session, gateway=gateway).post("/invoices", json=BODY, headers=AUTH)
    assert "req_fake123" in caplog.text
    assert "sk_test_fake_not_real" not in caplog.text


@db
def test_retry_after_db_failure_between_finalize_and_insert(
    session: Session, approved: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = FakeStripeGateway()
    client = make_client(session=session, gateway=gateway)
    real_store = invoices_api._store_invoice
    attempts = {"n": 0}

    def flaky(*args: Any, **kwargs: Any) -> Invoice:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("db went away")
        return real_store(*args, **kwargs)

    monkeypatch.setattr(invoices_api, "_store_invoice", flaky)

    with pytest.raises(RuntimeError):
        client.post("/invoices", json=BODY, headers=AUTH)
    session.rollback()
    assert "finalize_invoice" in gateway.methods()
    assert count(session, Invoice) == 0

    retry = client.post("/invoices", json=BODY, headers=AUTH)

    assert retry.status_code == 201
    assert count(session, Invoice) == 1
    assert gateway.count_created("create_customer") == 1
    assert gateway.count_created("create_invoice") == 1
    assert gateway.count_created("add_invoice_item") == 2
    assert gateway.count_created("finalize_invoice") == 1
    assert len(gateway.calls) == 10  # every call was replayed through its idempotency key


@db
@pytest.mark.parametrize(("winner_changes", "expected"), [({}, 200), ({"currency": "usd"}, 409)])
def test_concurrent_duplicate_reference_is_handled_not_500(
    session: Session, approved: None, winner_changes: dict[str, Any], expected: int
) -> None:
    gateway = FakeStripeGateway()

    def other_request_wins_the_race() -> None:
        # Runs after our existence check, before our insert.
        cust = Customer(stripe_customer_id="cus_other", name="Acme Ltd", email="ap@acme.example")
        session.add(cust)
        session.flush()
        winner = with_(**winner_changes)
        session.add(
            Invoice(
                customer_id=cust.id,
                stripe_invoice_id="in_other",
                reference=winner["reference"],
                amount_minor=255000,
                currency=winner["currency"],
                hosted_invoice_url="https://invoice.stripe.test/i/in_other",
                sent_on=None,
                due_on=date(2026, 11, 16),
                status=InvoiceStatus.OPEN,
                state=InvoiceState.ACTIVE,
            )
        )
        session.commit()

    gateway.before["finalize_invoice"] = other_request_wins_the_race
    resp = make_client(session=session, gateway=gateway).post("/invoices", json=BODY, headers=AUTH)

    assert resp.status_code == expected
    if expected == 200:
        assert resp.json()["stripe_invoice_id"] == "in_other"
    else:
        assert resp.json() == {"error": "reference_conflict"}
    assert count(session, Invoice) == 1
