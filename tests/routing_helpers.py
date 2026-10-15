"""Shared setup for the routing and webhook tests."""

from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from collection_agent.calendar import uk_calendar
from collection_agent.classifier import Classification
from collection_agent.db.models import InboundMessage, Invoice, OutboundEmail
from collection_agent.policy import ChasePolicy
from collection_agent.routing import RouteResult, route_reply
from collection_agent.stripe_gateway import StripeGateway
from tests.daily_helpers import IDENTITY, chased_invoice

TODAY = date(2026, 10, 15)  # a Thursday
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
CAL = uk_calendar([2026, 2027])
POLICY = ChasePolicy()


def make_message(
    session: Session,
    inv: Invoice | None,
    *,
    message_id: str = "reply-1@mail.acme.test",
    subject: str = "Invoice INV-1",
    references: str | None = None,
    received_at: datetime = NOW,
) -> InboundMessage:
    msg = InboundMessage(
        invoice_id=inv.id if inv else None,
        message_id=message_id,
        from_address="ap@acme.test",
        subject=subject,
        body_text="body",
        received_at=received_at,
        counts_as_response=True,
        references_header=references,
        match_method="reply_token",
        review_status="needs_review",
        route="classification_pending",
    )
    session.add(msg)
    session.flush()
    return msg


def money(inv: Invoice) -> tuple[int, str, date]:
    return inv.amount_minor, inv.currency, inv.due_on


def drafts(session: Session) -> list[OutboundEmail]:
    return list(session.scalars(select(OutboundEmail).order_by(OutboundEmail.created_at)))


def run_route(
    session: Session,
    inv: Invoice,
    msg: InboundMessage,
    classification: Classification,
    *,
    gateway: StripeGateway | None = None,
    policy: ChasePolicy | None = POLICY,
    identity: object = IDENTITY,
    today: date = TODAY,
    now: datetime = NOW,
    min_confidence: float = 0.6,
) -> RouteResult:
    """Route, and check on the way out that money fields never move."""
    before = money(inv)
    result = route_reply(
        session,
        msg,
        classification,
        inv,
        policy,
        CAL,
        gateway,
        identity,  # type: ignore[arg-type]
        today,
        now=now,
        min_confidence=min_confidence,
    )
    session.refresh(inv)
    assert money(inv) == before, "routing changed amount, currency or due date"
    assert all(
        d.status.value == "pending_approval"
        for d in drafts(session)
        if d.inbound_message_id == msg.id
    )
    return result


def open_invoice(session: Session, **kw: object) -> Invoice:
    return chased_invoice(session, **kw)
