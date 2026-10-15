"""Data access. Callers own commit/rollback; nothing here commits."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from collection_agent.chasing import (
    CustomerReply,
    InvoiceSnapshot,
    InvoiceState,
    InvoiceStatus,
    ReminderKind,
    ReminderSent,
)
from collection_agent.db.models import (
    ChasePolicyRow,
    InboundMessage,
    Invoice,
    PolicyStatus,
    Reminder,
)
from collection_agent.policy import ChasePolicy

LONDON = ZoneInfo("Europe/London")


class InvoiceNotFoundError(LookupError):
    pass


class InvoiceNotSentError(LookupError):
    pass


class NoApprovedPolicyError(LookupError):
    pass


class PolicyStateError(ValueError):
    pass


@dataclass(frozen=True)
class DecisionInputs:
    invoice: InvoiceSnapshot
    reminders: list[ReminderSent]
    replies: list[CustomerReply]


def load_decision_inputs(session: Session, invoice_id: uuid.UUID) -> DecisionInputs:
    inv = session.get(Invoice, invoice_id)
    if inv is None:
        raise InvoiceNotFoundError(f"invoice {invoice_id} does not exist")
    if inv.sent_on is None:
        raise InvoiceNotSentError(f"invoice {invoice_id} has not been sent yet")

    reminder_rows = session.scalars(
        select(Reminder).where(Reminder.invoice_id == invoice_id).order_by(Reminder.sent_on)
    )
    received = session.scalars(
        select(InboundMessage.received_at)
        .where(InboundMessage.invoice_id == invoice_id, InboundMessage.counts_as_response)
        .order_by(InboundMessage.received_at)
    )
    return DecisionInputs(
        invoice=InvoiceSnapshot(
            id=str(inv.id),
            status=inv.status,
            state=inv.state,
            sent_on=inv.sent_on,
            due_on=inv.due_on,
            paused_until=inv.paused_until,
            promised_date=inv.promised_date,
        ),
        reminders=[ReminderSent(kind=r.kind, sent_on=r.sent_on) for r in reminder_rows],
        replies=[CustomerReply(received_on=_london_date(at)) for at in received],
    )


def _london_date(at: datetime) -> date:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(LONDON).date()


def list_open_invoice_ids(session: Session) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(Invoice.id)
            .where(
                Invoice.status == InvoiceStatus.OPEN,
                Invoice.state == InvoiceState.ACTIVE,
                Invoice.sent_on.is_not(None),
            )
            .order_by(Invoice.sent_on, Invoice.id)
        )
    )


def record_reminder(
    session: Session,
    invoice_id: uuid.UUID,
    kind: ReminderKind,
    sent_on: date,
    *,
    rule_code: str | None = None,
    rule_label: str | None = None,
) -> None:
    session.add(
        Reminder(
            invoice_id=invoice_id,
            kind=kind,
            sent_on=sent_on,
            rule_code=rule_code,
            rule_label=rule_label,
        )
    )
    session.flush()


def record_inbound_message(
    session: Session,
    *,
    invoice_id: uuid.UUID | None,
    message_id: str,
    from_address: str,
    subject: str,
    body_text: str,
    received_at: datetime,
    counts_as_response: bool,
    postmark_message_id: str | None = None,
    match_method: str = "none",
    is_auto_reply: bool = False,
    classified_text: str | None = None,
    references_header: str | None = None,
    route: str | None = None,
    review_status: str = "not_needed",
    attachment_count: int = 0,
    id: uuid.UUID | None = None,
) -> bool:
    """Insert a message; return False (no error) if `message_id` was already stored. The caller
    may supply the row's `id` so it can refer to the message without a second query."""
    stmt = (
        pg_insert(InboundMessage)
        .values(
            id=id or uuid.uuid4(),
            invoice_id=invoice_id,
            message_id=message_id,
            from_address=from_address,
            subject=subject,
            body_text=body_text,
            received_at=received_at,
            counts_as_response=counts_as_response,
            postmark_message_id=postmark_message_id,
            match_method=match_method,
            is_auto_reply=is_auto_reply,
            classified_text=classified_text,
            references_header=references_header,
            route=route,
            review_status=review_status,
            attachment_count=attachment_count,
        )
        .on_conflict_do_nothing(index_elements=["message_id"])
        .returning(InboundMessage.id)
    )
    return session.execute(stmt).scalar_one_or_none() is not None


def get_approved_policy(session: Session) -> ChasePolicy:
    row = session.scalars(
        select(ChasePolicyRow).where(ChasePolicyRow.status == PolicyStatus.APPROVED)
    ).one_or_none()
    if row is None:
        raise NoApprovedPolicyError("no approved ChasePolicy; approve a draft before chasing")
    return ChasePolicy(
        no_response_days=row.no_response_days,
        clearance_day_of_month=row.clearance_day_of_month,
        clearance_month_offset=row.clearance_month_offset,
        pre_deadline_buffer_working_days=row.pre_deadline_buffer_working_days,
        max_reminders=row.max_reminders,
        promise_grace_working_days=row.promise_grace_working_days,
    )


def create_draft_policy(session: Session, policy: ChasePolicy) -> int:
    current_max = session.scalar(select(func.max(ChasePolicyRow.version)))
    version = (current_max or 0) + 1
    session.add(
        ChasePolicyRow(
            version=version,
            status=PolicyStatus.DRAFT,
            **policy.model_dump(),
        )
    )
    session.flush()
    return version


def approve_policy(session: Session, version: int, approved_by: str) -> None:
    row = session.scalars(
        select(ChasePolicyRow).where(ChasePolicyRow.version == version).with_for_update()
    ).one_or_none()
    if row is None:
        raise PolicyStateError(f"policy version {version} does not exist")
    if row.status is not PolicyStatus.DRAFT:
        raise PolicyStateError(
            f"policy version {version} is {row.status.value}; only a draft can be approved"
        )
    session.execute(
        update(ChasePolicyRow)
        .where(ChasePolicyRow.status == PolicyStatus.APPROVED)
        .values(status=PolicyStatus.SUPERSEDED)
    )
    row.status = PolicyStatus.APPROVED
    row.approved_by = approved_by
    row.approved_at = datetime.now(UTC)
    session.flush()
