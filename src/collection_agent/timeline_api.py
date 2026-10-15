"""GET /invoices/{id}/timeline: one chronological list derived from the existing tables.

There is no audit table. Every event's time is the row's own insertion or action time, so the
list reads in the order things really happened even when the demo clock has moved the business
date around (an inbound message's `received_at` is on the demo clock, so it is shown in the
detail, not used for ordering).
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceState
from collection_agent.db.models import (
    Contract,
    InboundMessage,
    Invoice,
    OutboundEmail,
    OutboundStatus,
    Reminder,
    StripeEvent,
)
from collection_agent.deps import AdminDep, SessionDep
from collection_agent.templates import format_date

router = APIRouter()

DETAIL_CHARS = 240


class TimelineEvent(BaseModel):
    at: datetime
    type: str
    title: str
    detail: str | None = None
    tags: list[str] = []


_KIND_NAMES = {
    "invoice": "Invoice email",
    "reminder": "Reminder",
    "reminder_pre_deadline": "Pre-deadline reminder",
    "reminder_broken_promise": "Broken-promise reminder",
    "reply_paid_thanks": "Thank-you reply",
    "reply_remittance_request": "Remittance request",
    "reply_promise_ack": "Promise acknowledgement",
    "reply_holding": "Holding reply",
    "reply_resend": "Invoice resend",
}
_STRIPE_TITLES = {
    "invoice.paid": "Paid in Stripe",
    "invoice.payment_failed": "Payment failed in Stripe",
    "invoice.voided": "Voided in Stripe",
    "invoice.marked_uncollectible": "Marked uncollectible in Stripe",
}


def _kind_name(kind: str) -> str:
    return _KIND_NAMES.get(kind, kind.replace("_", " ").capitalize())


def _intent_name(intent: str) -> str:
    return intent.replace("_", " ").capitalize()


def _clip(text: str | None) -> str | None:
    if not text:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= DETAIL_CHARS else flat[: DETAIL_CHARS - 1] + "…"


def _outbox_events(row: OutboundEmail, drafted_at: datetime) -> list[TimelineEvent]:
    name = _kind_name(row.kind)
    tags = [row.rule_label] if row.rule_label else []
    detail = _clip(f"{row.kind} · {row.subject}")
    events = [
        TimelineEvent(
            at=drafted_at,
            type="email_drafted",
            title=f"{name} drafted",
            detail=detail,
            tags=tags,
        )
    ]
    if row.approved_at is not None:
        who = f"approved by {row.approved_by}" if row.approved_by else None
        events.append(
            TimelineEvent(
                at=row.approved_at,
                type="email_approved",
                title=f"{name} approved",
                detail=who,
                tags=tags,
            )
        )
    if row.status is OutboundStatus.SENT and row.sent_at is not None:
        events.append(
            TimelineEvent(
                at=row.sent_at,
                type="email_sent",
                title=f"{name} sent",
                detail=f"to {row.to_address}",
                tags=tags,
            )
        )
    elif row.status is OutboundStatus.REJECTED:
        reason = (
            f"{row.reject_reason} (by {row.rejected_by})" if row.rejected_by else row.reject_reason
        )
        events.append(
            TimelineEvent(
                at=row.rejected_at or row.updated_at,
                type="email_rejected",
                title=f"{name} rejected",
                detail=_clip(reason),
                tags=tags,
            )
        )
    elif row.status is OutboundStatus.FAILED:
        events.append(
            TimelineEvent(
                at=row.updated_at,
                type="email_failed",
                title=f"{name} failed to send",
                detail=_clip(row.error),
                tags=tags,
            )
        )
    return events


def _inbound_events(m: InboundMessage) -> list[TimelineEvent]:
    received = f"received {m.received_at.date().isoformat()} · " if m.received_at else ""
    events = [
        TimelineEvent(
            at=m.created_at,
            type="reply_received",
            title="Customer reply received",
            detail=_clip(m.classified_text or m.body_text) or _clip(received + m.subject),
        )
    ]
    if m.intent is not None:
        confidence = (
            f" (confidence {m.intent_confidence:.2f})" if m.intent_confidence is not None else ""
        )
        events.append(
            TimelineEvent(
                at=m.created_at,
                type="reply_classified",
                title=f"{_intent_name(m.intent)}{confidence}",
            )
        )
    if m.route is not None and m.route != "classification_pending":
        needs = ["needs review"] if m.review_status == "needs_review" else []
        events.append(
            TimelineEvent(
                at=m.created_at, type="reply_routed", title="Routed", detail=m.route, tags=needs
            )
        )
    elif m.route == "classification_pending":
        events.append(
            TimelineEvent(
                at=m.created_at,
                type="reply_routed",
                title="Routed",
                detail=m.route,
                tags=["needs review"],
            )
        )
    return events


def _state_events(inv: Invoice, inbound: list[InboundMessage]) -> list[TimelineEvent]:
    def anchor(*routes: str) -> datetime:
        matching = [m.created_at for m in inbound if m.route in routes]
        return max(matching) if matching else inv.updated_at

    events: list[TimelineEvent] = []
    if inv.promised_date is not None:
        events.append(
            TimelineEvent(
                at=anchor("promise_accepted"),
                type="promise_recorded",
                title=f"Promised to pay by {format_date(inv.promised_date)}",
            )
        )
    if inv.paused_until is not None:
        events.append(
            TimelineEvent(
                at=anchor("promise_accepted"),
                type="paused",
                title=f"Chasing paused until {format_date(inv.paused_until)}",
            )
        )
    if inv.state is InvoiceState.DISPUTED:
        events.append(
            TimelineEvent(
                at=anchor("dispute"), type="disputed", title="Disputed: passed to a person"
            )
        )
    elif inv.state is InvoiceState.WITH_HUMAN:
        events.append(
            TimelineEvent(
                at=anchor("admin_billing_change"), type="with_human", title="Passed to a person"
            )
        )
    elif inv.state is InvoiceState.ESCALATED:
        events.append(
            TimelineEvent(
                at=inv.escalated_at or inv.updated_at,
                type="escalated",
                title="Escalated to a person",
                tags=[inv.escalation_rule_label] if inv.escalation_rule_label else [],
            )
        )
    return events


def build_timeline(session: Session, inv: Invoice) -> list[TimelineEvent]:
    events = [
        TimelineEvent(
            at=inv.created_at,
            type="invoice_created",
            title=f"Invoice {inv.reference} created",
            detail=f"due {format_date(inv.due_on)}",
        )
    ]
    contract = session.execute(
        select(Contract.filename, Contract.created_at).where(Contract.invoice_id == inv.id)
    ).one_or_none()
    if contract is not None:
        events.append(
            TimelineEvent(
                at=contract.created_at,
                type="contract_uploaded",
                title="Contract uploaded",
                detail=contract.filename,
            )
        )
    outbox = list(
        session.scalars(
            select(OutboundEmail)
            .where(OutboundEmail.invoice_id == inv.id)
            .order_by(OutboundEmail.created_at, OutboundEmail.id)
        )
    )
    for row in outbox:
        if row.inbound_message_id is None:
            events.extend(_outbox_events(row, row.created_at))
    for reminder in session.scalars(
        select(Reminder).where(Reminder.invoice_id == inv.id).order_by(Reminder.created_at)
    ):
        events.append(
            TimelineEvent(
                at=reminder.created_at,
                type="reminder_recorded",
                title=f"Reminder recorded ({reminder.kind.value.replace('_', ' ')})",
                detail=f"sent on {format_date(reminder.sent_on)}",
                tags=[reminder.rule_label] if reminder.rule_label else [],
            )
        )
    inbound = list(
        session.scalars(
            select(InboundMessage)
            .where(InboundMessage.invoice_id == inv.id)
            .order_by(InboundMessage.created_at, InboundMessage.id)
        )
    )
    for message in inbound:
        events.extend(_inbound_events(message))
    events.extend(_state_events(inv, inbound))
    # A reply draft's own `created_at` is the injected clock (so the daily reply cap is
    # testable), which in demo mode is the simulated date. Order it by the message it answers.
    received = {m.id: m.created_at for m in inbound}
    for row in outbox:
        if row.inbound_message_id is not None:
            events.extend(_outbox_events(row, received.get(row.inbound_message_id, row.created_at)))
    for stripe_event in session.scalars(
        select(StripeEvent)
        .where(StripeEvent.stripe_object_id == inv.stripe_invoice_id)
        .order_by(StripeEvent.created_at)
    ):
        events.append(
            TimelineEvent(
                at=stripe_event.created_at,
                type="stripe_event",
                title=_STRIPE_TITLES.get(stripe_event.type, f"Stripe: {stripe_event.type}"),
                tags=[stripe_event.type],
            )
        )
    events.sort(key=lambda e: e.at)  # stable: ties keep the order they were added in
    return events


@router.get("/invoices/{invoice_id}/timeline", response_model=None)
def get_timeline(invoice_id: uuid.UUID, _admin: AdminDep, session: SessionDep) -> Response:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        return JSONResponse({"error": "invoice_not_found"}, status_code=404)
    return JSONResponse([e.model_dump(mode="json") for e in build_timeline(session, inv)])
