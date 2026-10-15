"""Shared outbox helpers: building a draft row and applying the effects of a completed send."""

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from collection_agent.db.models import (
    REMINDER_OUTBOUND_KINDS,
    Customer,
    Invoice,
    OutboundEmail,
    OutboundKind,
    OutboundStatus,
)
from collection_agent.db.repository import record_reminder
from collection_agent.deps import EmailIdentity


def new_outbound_email(
    *,
    invoice: Invoice,
    customer: Customer,
    identity: EmailIdentity,
    kind: OutboundKind,
    subject: str,
    body: str,
    attach_contract: bool,
    in_reply_to_message_id: str | None = None,
    references: str | None = None,
    inbound_message_id: uuid.UUID | None = None,
    rule_code: str | None = None,
    rule_label: str | None = None,
) -> OutboundEmail:
    """A `pending_approval` outbox row. From and Reply-To are the same for every kind: the
    reply address carries the invoice's token so replies map back to the invoice."""
    return OutboundEmail(
        invoice_id=invoice.id,
        kind=kind.value,
        to_address=customer.email,
        from_address=identity.from_address,
        from_name=identity.from_name,
        reply_to=f"reply+{invoice.reply_token}@{identity.reply_to_domain}",
        subject=subject,
        body_text=body,
        attach_contract=attach_contract,
        status=OutboundStatus.PENDING_APPROVAL,
        in_reply_to_message_id=in_reply_to_message_id,
        references=references,
        inbound_message_id=inbound_message_id,
        rule_code=rule_code,
        rule_label=rule_label,
    )


def apply_sent(
    session: Session,
    row: OutboundEmail,
    invoice: Invoice,
    *,
    today: date,
    provider_message_id: str | None,
) -> None:
    """Mark `row` sent and apply what a send means. Does not commit, so the caller's commit
    makes the status change and its effects atomic: the invoice email stamps `sent_on` (if
    unset); a reminder is recorded in `reminders`, which feeds the next chase decision. A reply
    to a customer's email is only marked sent: it is not a chase, so it records nothing."""
    row.status = OutboundStatus.SENT
    row.sent_at = datetime.now(UTC)
    row.provider_message_id = provider_message_id
    kind = OutboundKind(row.kind)
    if kind is OutboundKind.INVOICE:
        if invoice.sent_on is None:
            invoice.sent_on = today
    elif kind in REMINDER_OUTBOUND_KINDS:
        record_reminder(
            session,
            invoice.id,
            REMINDER_OUTBOUND_KINDS[kind],
            today,
            rule_code=row.rule_code,
            rule_label=row.rule_label,
        )
