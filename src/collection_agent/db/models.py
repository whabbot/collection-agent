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
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
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
    REMINDER = "reminder"
    REMINDER_PRE_DEADLINE = "reminder_pre_deadline"
    REMINDER_BROKEN_PROMISE = "reminder_broken_promise"
    REPLY_PAID_THANKS = "reply_paid_thanks"
    REPLY_REMITTANCE_REQUEST = "reply_remittance_request"
    REPLY_PROMISE_ACK = "reply_promise_ack"
    REPLY_HOLDING = "reply_holding"
    REPLY_RESEND = "reply_resend"


REPLY_OUTBOUND_KINDS: frozenset[OutboundKind] = frozenset(
    {
        OutboundKind.REPLY_PAID_THANKS,
        OutboundKind.REPLY_REMITTANCE_REQUEST,
        OutboundKind.REPLY_PROMISE_ACK,
        OutboundKind.REPLY_HOLDING,
        OutboundKind.REPLY_RESEND,
    }
)

REMINDER_OUTBOUND_KINDS: dict[OutboundKind, ReminderKind] = {
    OutboundKind.REMINDER: ReminderKind.REMINDER,
    OutboundKind.REMINDER_PRE_DEADLINE: ReminderKind.PRE_DEADLINE,
    OutboundKind.REMINDER_BROKEN_PROMISE: ReminderKind.BROKEN_PROMISE,
}


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
    escalated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    escalation_rule_code: Mapped[str | None] = mapped_column(String)
    escalation_rule_label: Mapped[str | None] = mapped_column(String)
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
    rule_code: Mapped[str | None] = mapped_column(String)
    rule_label: Mapped[str | None] = mapped_column(String)


MATCH_METHODS = ("reply_token", "in_reply_to", "sender", "none")
REVIEW_STATUSES = ("not_needed", "needs_review", "reviewed")
INTENT_VALUES = (
    "already_paid",
    "promise_to_pay",
    "request_extension",
    "request_payment_plan",
    "dispute",
    "admin_request",
    "contract_question",
    "out_of_office",
    "other",
)


def sql_in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class InboundMessage(Base):
    __tablename__ = "inbound_messages"
    __table_args__ = (
        CheckConstraint(sql_in("match_method", MATCH_METHODS), name="match_method_known"),
        CheckConstraint(sql_in("review_status", REVIEW_STATUSES), name="review_status_known"),
        CheckConstraint(
            "intent IS NULL OR " + sql_in("intent", INTENT_VALUES), name="intent_known"
        ),
        CheckConstraint(
            "reviewer_intent IS NULL OR " + sql_in("reviewer_intent", INTENT_VALUES),
            name="reviewer_intent_known",
        ),
        Index("ix_inbound_messages_invoice_id", "invoice_id"),
        Index("ix_inbound_messages_review_status", "review_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("invoices.id"))
    message_id: Mapped[str] = mapped_column(String, unique=True)
    from_address: Mapped[str] = mapped_column(String)
    subject: Mapped[str] = mapped_column(String)
    body_text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    counts_as_response: Mapped[bool]
    postmark_message_id: Mapped[str | None] = mapped_column(String)
    match_method: Mapped[str] = mapped_column(String, server_default="none")
    is_auto_reply: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # The text sent to the classifier (quotes stripped, truncated), and the sender's own
    # `References` header, kept so a reply can thread correctly.
    classified_text: Mapped[str | None] = mapped_column(Text)
    references_header: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String)
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    intent_probabilities: Mapped[dict[str, float] | None] = mapped_column(JSONB)
    classifier_model: Mapped[str | None] = mapped_column(String)
    promised_date: Mapped[date | None] = mapped_column(Date)
    promised_date_raw: Mapped[dict[str, str] | None] = mapped_column(JSONB)
    route: Mapped[str | None] = mapped_column(String)
    review_status: Mapped[str] = mapped_column(String, server_default="not_needed")
    reviewed_by: Mapped[str | None] = mapped_column(String)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer_intent: Mapped[str | None] = mapped_column(String)
    review_note: Mapped[str | None] = mapped_column(Text)
    attachment_count: Mapped[int] = mapped_column(Integer, server_default="0")


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
        CheckConstraint(
            sql_in("kind", tuple(k.value for k in OutboundKind)),
            name="kind_known",
        ),
        Index("ix_outbound_emails_status", "status"),
        # At most one live invoice email per invoice: the guard against sending it twice.
        Index(
            "uq_outbound_emails_one_live_invoice_email",
            "invoice_id",
            unique=True,
            postgresql_where=text("kind = 'invoice' AND status NOT IN ('rejected', 'failed')"),
        ),
        # At most one reminder per invoice waiting for approval or being sent: stops the daily
        # run drafting a second reminder while one is outstanding.
        Index(
            "uq_outbound_emails_one_active_reminder",
            "invoice_id",
            unique=True,
            postgresql_where=text(
                "kind IN ('reminder', 'reminder_pre_deadline', 'reminder_broken_promise') "
                "AND status IN ('pending_approval', 'sending')"
            ),
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
    rule_code: Mapped[str | None] = mapped_column(String)
    rule_label: Mapped[str | None] = mapped_column(String)
    approved_by: Mapped[str | None] = mapped_column(String)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reject_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String)
    error: Mapped[str | None] = mapped_column(Text)
    # Threading for reply kinds: header values, angle brackets included.
    in_reply_to_message_id: Mapped[str | None] = mapped_column(Text)
    references: Mapped[str | None] = mapped_column(Text)
    inbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("inbound_messages.id"), index=True
    )
    resolved_by: Mapped[str | None] = mapped_column(String)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DailyRun(Base):
    """One row per (non-dry) execution of the daily job."""

    __tablename__ = "daily_runs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_date: Mapped[date] = mapped_column(Date)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB)
