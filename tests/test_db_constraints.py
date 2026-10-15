import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collection_agent.db.models import ChasePolicyRow, PolicyStatus
from tests.db_helpers import make_customer, make_invoice

pytestmark = pytest.mark.db

VALID_POLICY = {
    "no_response_days": 7,
    "clearance_day_of_month": 15,
    "clearance_month_offset": 1,
    "pre_deadline_buffer_working_days": 3,
    "max_reminders": 3,
    "promise_grace_working_days": 2,
}


def _policy(version: int, status: PolicyStatus, **overrides: int) -> ChasePolicyRow:
    return ChasePolicyRow(version=version, status=status, **{**VALID_POLICY, **overrides})


def test_duplicate_stripe_invoice_id_rejected(session: Session) -> None:
    cust = make_customer(session)
    make_invoice(session, stripe_invoice_id="in_dup", customer=cust)
    with pytest.raises(IntegrityError), session.begin_nested():
        make_invoice(session, stripe_invoice_id="in_dup", customer=cust)


@pytest.mark.parametrize("amount", [0, -1])
def test_non_positive_amount_rejected(session: Session, amount: int) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        make_invoice(session, amount_minor=amount)


def test_reply_token_is_generated_and_unique(session: Session) -> None:
    a = make_invoice(session)
    b = make_invoice(session)
    assert a.reply_token and b.reply_token and a.reply_token != b.reply_token


def test_second_approved_policy_rejected(session: Session) -> None:
    session.add(_policy(1, PolicyStatus.APPROVED))
    session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_policy(2, PolicyStatus.APPROVED))
        session.flush()


def test_multiple_non_approved_policies_allowed(session: Session) -> None:
    session.add_all(
        [_policy(1, PolicyStatus.DRAFT), _policy(2, PolicyStatus.DRAFT)]
        + [_policy(3, PolicyStatus.SUPERSEDED), _policy(4, PolicyStatus.APPROVED)]
    )
    session.flush()
    assert session.execute(text("select count(*) from chase_policies")).scalar_one() == 4


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("no_response_days", 0),
        ("clearance_day_of_month", 0),
        ("clearance_day_of_month", 29),
        ("clearance_month_offset", 0),
        ("pre_deadline_buffer_working_days", 0),
        ("max_reminders", 0),
        ("promise_grace_working_days", 0),
    ],
)
def test_invalid_policy_numbers_rejected(session: Session, field: str, value: int) -> None:
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(_policy(1, PolicyStatus.DRAFT, **{field: value}))
        session.flush()
