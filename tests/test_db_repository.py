import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent.calendar import WorkingCalendar
from collection_agent.chasing import (
    Action,
    InvoiceState,
    InvoiceStatus,
    ReminderKind,
    decide_next_action,
)
from collection_agent.db.models import ChasePolicyRow, InboundMessage, PolicyStatus
from collection_agent.db.repository import (
    InvoiceNotFoundError,
    InvoiceNotSentError,
    NoApprovedPolicyError,
    PolicyStateError,
    approve_policy,
    create_draft_policy,
    get_approved_policy,
    list_open_invoice_ids,
    load_decision_inputs,
    record_inbound_message,
    record_reminder,
)
from collection_agent.policy import ChasePolicy
from tests.db_helpers import make_invoice

pytestmark = pytest.mark.db

CAL = WorkingCalendar(frozenset({date(2026, 12, 25), date(2026, 12, 28), date(2026, 10, 26)}))


def add_msg(
    session: Session,
    invoice_id: uuid.UUID | None,
    received_at: datetime,
    *,
    message_id: str | None = None,
    counts: bool = True,
) -> bool:
    return record_inbound_message(
        session,
        invoice_id=invoice_id,
        message_id=message_id or f"<{uuid.uuid4()}@mail.test>",
        from_address="ap@acme.test",
        subject="Re: invoice",
        body_text="body",
        received_at=received_at,
        counts_as_response=counts,
    )


# --- load_decision_inputs ---------------------------------------------------


def test_load_decision_inputs_maps_all_fields(session: Session) -> None:
    inv = make_invoice(
        session,
        sent_on=date(2026, 10, 1),
        due_on=date(2026, 11, 20),
        paused_until=date(2026, 10, 9),
        promised_date=date(2026, 10, 12),
        state=InvoiceState.ACTIVE,
    )
    record_reminder(session, inv.id, ReminderKind.REMINDER, date(2026, 10, 8))
    record_reminder(session, inv.id, ReminderKind.PRE_DEADLINE, date(2026, 11, 11))
    add_msg(session, inv.id, datetime(2026, 10, 5, 10, 0, tzinfo=UTC))

    got = load_decision_inputs(session, inv.id)

    assert got.invoice.id == str(inv.id)
    assert got.invoice.status is InvoiceStatus.OPEN
    assert got.invoice.state is InvoiceState.ACTIVE
    assert got.invoice.sent_on == date(2026, 10, 1)
    assert got.invoice.due_on == date(2026, 11, 20)
    assert got.invoice.paused_until == date(2026, 10, 9)
    assert got.invoice.promised_date == date(2026, 10, 12)
    assert [(r.kind, r.sent_on) for r in got.reminders] == [
        (ReminderKind.REMINDER, date(2026, 10, 8)),
        (ReminderKind.PRE_DEADLINE, date(2026, 11, 11)),
    ]
    assert [r.received_on for r in got.replies] == [date(2026, 10, 5)]


def test_load_decision_inputs_excludes_non_responses_and_other_invoices(session: Session) -> None:
    inv = make_invoice(session)
    other = make_invoice(session)
    add_msg(session, inv.id, datetime(2026, 10, 5, 10, 0, tzinfo=UTC), counts=False)
    add_msg(session, other.id, datetime(2026, 10, 6, 10, 0, tzinfo=UTC))
    add_msg(session, None, datetime(2026, 10, 7, 10, 0, tzinfo=UTC))
    assert load_decision_inputs(session, inv.id).replies == []


def test_reply_date_is_london_local_in_bst(session: Session) -> None:
    inv = make_invoice(session)
    add_msg(session, inv.id, datetime(2026, 10, 5, 23, 30, tzinfo=UTC))  # BST: 00:30 on the 6th
    assert [r.received_on for r in load_decision_inputs(session, inv.id).replies] == [
        date(2026, 10, 6)
    ]


def test_reply_date_is_london_local_in_gmt(session: Session) -> None:
    inv = make_invoice(session)
    add_msg(session, inv.id, datetime(2027, 1, 12, 23, 30, tzinfo=UTC))  # GMT: same day
    assert [r.received_on for r in load_decision_inputs(session, inv.id).replies] == [
        date(2027, 1, 12)
    ]


def test_load_decision_inputs_unknown_invoice_raises(session: Session) -> None:
    with pytest.raises(InvoiceNotFoundError, match="does not exist"):
        load_decision_inputs(session, uuid.uuid4())


def test_load_decision_inputs_unsent_invoice_raises(session: Session) -> None:
    inv = make_invoice(session, sent_on=None)
    with pytest.raises(InvoiceNotSentError, match="not been sent"):
        load_decision_inputs(session, inv.id)


# --- list_open_invoice_ids --------------------------------------------------


def test_list_open_invoice_ids_only_open_and_active(session: Session) -> None:
    open_inv = make_invoice(session)
    make_invoice(session, status=InvoiceStatus.PAID)
    make_invoice(session, status=InvoiceStatus.VOID)
    make_invoice(session, state=InvoiceState.DISPUTED)
    make_invoice(session, state=InvoiceState.WITH_HUMAN)
    make_invoice(session, state=InvoiceState.ESCALATED)
    assert list_open_invoice_ids(session) == [open_inv.id]


def test_list_open_invoice_ids_excludes_unsent(session: Session) -> None:
    sent = make_invoice(session)
    make_invoice(session, sent_on=None)
    assert list_open_invoice_ids(session) == [sent.id]


# --- record_inbound_message -------------------------------------------------


def test_duplicate_message_id_returns_false_and_keeps_one_row(session: Session) -> None:
    inv = make_invoice(session)
    at = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    assert add_msg(session, inv.id, at, message_id="<dup@mail.test>") is True
    assert add_msg(session, inv.id, at, message_id="<dup@mail.test>") is False
    count = session.scalar(
        select(func.count())
        .select_from(InboundMessage)
        .where(InboundMessage.message_id == "<dup@mail.test>")
    )
    assert count == 1


def test_unmatched_message_with_null_invoice_is_stored(session: Session) -> None:
    assert add_msg(session, None, datetime(2026, 10, 5, 10, 0, tzinfo=UTC)) is True


# --- policy lifecycle -------------------------------------------------------


def test_policy_draft_approve_get(session: Session) -> None:
    policy = ChasePolicy(no_response_days=10, max_reminders=4)
    version = create_draft_policy(session, policy)
    assert version == 1
    with pytest.raises(NoApprovedPolicyError):
        get_approved_policy(session)
    approve_policy(session, version, "alice")
    assert get_approved_policy(session) == policy
    row = session.scalars(select(ChasePolicyRow)).one()
    assert row.status is PolicyStatus.APPROVED
    assert row.approved_by == "alice"
    assert row.approved_at is not None


def test_approving_v2_supersedes_v1(session: Session) -> None:
    v1 = create_draft_policy(session, ChasePolicy())
    approve_policy(session, v1, "alice")
    v2 = create_draft_policy(session, ChasePolicy(no_response_days=5))
    assert v2 == 2
    approve_policy(session, v2, "bob")
    assert get_approved_policy(session).no_response_days == 5
    statuses = {r.version: r.status for r in session.scalars(select(ChasePolicyRow)).all()}
    assert statuses == {1: PolicyStatus.SUPERSEDED, 2: PolicyStatus.APPROVED}


def test_approving_non_draft_raises(session: Session) -> None:
    v1 = create_draft_policy(session, ChasePolicy())
    approve_policy(session, v1, "alice")
    with pytest.raises(PolicyStateError, match="only a draft"):
        approve_policy(session, v1, "alice")
    v2 = create_draft_policy(session, ChasePolicy())
    approve_policy(session, v2, "bob")
    with pytest.raises(PolicyStateError, match="only a draft"):
        approve_policy(session, v1, "carol")


def test_approving_unknown_version_raises(session: Session) -> None:
    with pytest.raises(PolicyStateError, match="does not exist"):
        approve_policy(session, 99, "alice")


def test_no_approved_policy_raises(session: Session) -> None:
    create_draft_policy(session, ChasePolicy())
    with pytest.raises(NoApprovedPolicyError, match="no approved"):
        get_approved_policy(session)


# --- end to end -------------------------------------------------------------


def _decide(session: Session, invoice_id: uuid.UUID, today: date) -> Action | None:
    policy = get_approved_policy(session)
    got = load_decision_inputs(session, invoice_id)
    return decide_next_action(got.invoice, policy, got.reminders, got.replies, today, CAL)


def test_end_to_end_load_then_decide(session: Session) -> None:
    approve_policy(session, create_draft_policy(session, ChasePolicy()), "alice")
    inv = make_invoice(session, sent_on=date(2026, 10, 1))

    # Seven days of silence after sending: first reminder is due.
    assert _decide(session, inv.id, date(2026, 10, 8)) is Action.REMIND

    # An out-of-office does not count as a response.
    add_msg(session, inv.id, datetime(2026, 10, 6, 9, 0, tzinfo=UTC), counts=False)
    assert _decide(session, inv.id, date(2026, 10, 8)) is Action.REMIND

    # A real reply on the 6th resets the silence timer.
    add_msg(session, inv.id, datetime(2026, 10, 6, 9, 0, tzinfo=UTC))
    assert _decide(session, inv.id, date(2026, 10, 8)) is None
    assert _decide(session, inv.id, date(2026, 10, 13)) is Action.REMIND

    # Once a reminder is recorded, the next one waits another seven days.
    record_reminder(session, inv.id, ReminderKind.REMINDER, date(2026, 10, 13))
    assert _decide(session, inv.id, date(2026, 10, 14)) is None
