"""SQLAlchemy 2 models. Money is integer minor units plus currency; never floats."""

import secrets
import uuid
from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from collection_agent.chasing import InvoiceState, InvoiceStatus, ReminderKind


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    SUPERSEDED = "superseded"


class OutboundStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    SENDING = "sending"
    SENT = "sent"
    REJECTED = "rejected"
    FAILED = "failed"


class OutboundKind(StrEnum):
    """Extendable: add a value here, to the CHECK constraint and in a new migration."""

    INVOICE = "invoice"


def _pg_enum[E: StrEnum](enum_cls: type[E], name: str) -> Enum:
    return Enum(enum_cls, name=name, values_callable=lambda e: [m.value for m in e])


def _new_reply_token() -> str:
    return secrets.token_hex(16)


class Base(DeclarativeBase):
    pass


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    stripe_customer_id: Mapped[str] = mapped_column(String, unique=True)
    name: Mapped[str] = mapped_column(String)
    email: Mapped[str] = mapped_column(String)


class Invoice(Base):
    __tablename__ = "invoices"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="amount_minor_positive"),
        CheckConstraint("char_length(currency) = 3", name="currency_len_3"),
        Index("ix_invoices_status_state", "status", "state"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id"))
    stripe_invoice_id: Mapped[str] = mapped_column(String, unique=True)
    reference: Mapped[str] = mapped_column(String, unique=True)
    amount_minor: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    hosted_invoice_url: Mapped[str | None] = mapped_column(String)
    contract_id: Mapped[str | None] = mapped_column(String)
    sent_on: Mapped[date | None] = mapped_column(Date)
    due_on: Mapped[date] = mapped_column(Date)
    status: Mapped[InvoiceStatus] = mapped_column(_pg_enum(InvoiceStatus, "invoice_status"))
    state: Mapped[InvoiceState] = mapped_column(_pg_enum(InvoiceState, "invoice_state"))
    paused_until: Mapped[date | None] = mapped_column(Date)
    promised_date: Mapped[date | None] = mapped_column(Date)
    reply_token: Mapped[str] = mapped_column(String, unique=True, default=_new_reply_token)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Reminder(Base):
    __tablename__ = "reminders"
    __table_args__ = (Index("ix_reminders_invoice_id", "invoice_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"))
    kind: Mapped[ReminderKind] = mapped_column(_pg_enum(ReminderKind, "reminder_kind"))
    sent_on: Mapped[date] = mapped_column(Date)


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (Index("ix_inbound_messages_invoice_id", "invoice_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    message_id: Mapped[str] = mapped_column(String, unique=True)
    from_address: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    body_text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    counts_as_response: Mapped[bool]


class ChasePolicyRow(Base):
    __tablename__ = "chase_policies"
    __table_args__ = (
        CheckConstraint("no_response_days > 0", name="no_response_days_positive"),
        CheckConstraint(
            "clearance_day_of_month BETWEEN 1 AND 28", name="clearance_day_of_month_range"
        ),
        CheckConstraint("clearance_month_offset > 0", name="clearance_month_offset_positive"),
        CheckConstraint(
            "pre_deadline_buffer_working_days > 0", name="pre_deadline_buffer_positive"
        ),
        CheckConstraint("max_reminders > 0", name="max_reminders_positive"),
        CheckConstraint("promise_grace_working_days > 0", name="promise_grace_positive"),
        Index(
            "uq_chase_policies_one_approved",
            "status",
            unique=True,
            postgresql_where=text("status = 'approved'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    version: Mapped[int] = mapped_column(Integer, unique=True)
    no_response_days: Mapped[int] = mapped_column(Integer)
    clearance_day_of_month: Mapped[int] = mapped_column(Integer)
    clearance_month_offset: Mapped[int] = mapped_column(Integer)
    pre_deadline_buffer_working_days: Mapped[int] = mapped_column(Integer)
    max_reminders: Mapped[int] = mapped_column(Integer)
    promise_grace_working_days: Mapped[int] = mapped_column(Integer)
    status: Mapped[PolicyStatus] = mapped_column(_pg_enum(PolicyStatus, "policy_status"))
    approved_by: Mapped[str | None] = mapped_column(String)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StripeEvent(Base):
    """Every Stripe webhook event we have accepted; `event_id` makes delivery idempotent."""

    __tablename__ = "stripe_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    event_id: Mapped[str] = mapped_column(String, unique=True)
    type: Mapped[str] = mapped_column(String)
    stripe_object_id: Mapped[str | None] = mapped_column(String)
    livemode: Mapped[bool] = mapped_column(Boolean)


class Contract(Base):
    """The one current contract PDF for an invoice."""

    __tablename__ = "contracts"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"), unique=True)
    filename: Mapped[str] = mapped_column(String)
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)


class OutboundEmail(Base):
    """The outbox: every email waits here for a human to approve it before it is sent."""

    __tablename__ = "outbound_emails"
    __table_args__ = (
        CheckConstraint("kind IN ('invoice')", name="kind_known"),
        Index("ix_outbound_emails_status", "status"),
        # At most one live invoice email per invoice: the guard against sending it twice.
        Index(
            "uq_outbound_emails_one_live_invoice_email",
            "invoice_id",
            unique=True,
            postgresql_where=text("kind = 'invoice' AND status NOT IN ('rejected', 'failed')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    invoice_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("invoices.id"))
    kind: Mapped[str] = mapped_column(String)
    to_address: Mapped[str] = mapped_column(String)
    from_address: Mapped[str] = mapped_column(String)
    from_name: Mapped[str] = mapped_column(String)
    reply_to: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    body_text: Mapped[str] = mapped_column(Text)
    attach_contract: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[OutboundStatus] = mapped_column(_pg_enum(OutboundStatus, "outbound_status"))
    approved_by: Mapped[str | None] = mapped_column(String)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
