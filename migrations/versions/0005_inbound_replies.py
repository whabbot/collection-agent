"""inbound replies: classification and review columns, reply outbox kinds, threading

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15 23:59:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REPLY_KINDS = (
    "reply_paid_thanks",
    "reply_remittance_request",
    "reply_promise_ack",
    "reply_holding",
    "reply_resend",
)
_OLD_KINDS = ("invoice", "reminder", "reminder_pre_deadline", "reminder_broken_promise")
_INTENTS = (
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


def _in(column: str, values: Sequence[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("kind_known", "outbound_emails", type_="check")
    op.create_check_constraint(
        "kind_known", "outbound_emails", sa.text(_in("kind", (*_OLD_KINDS, *_REPLY_KINDS)))
    )
    op.add_column("outbound_emails", sa.Column("in_reply_to_message_id", sa.Text(), nullable=True))
    op.add_column("outbound_emails", sa.Column("references", sa.Text(), nullable=True))
    op.add_column("outbound_emails", sa.Column("inbound_message_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "outbound_emails_inbound_message_id_fkey",
        "outbound_emails",
        "inbound_messages",
        ["inbound_message_id"],
        ["id"],
    )
    op.create_index(
        "ix_outbound_emails_inbound_message_id", "outbound_emails", ["inbound_message_id"]
    )

    op.add_column("inbound_messages", sa.Column("postmark_message_id", sa.String(), nullable=True))
    op.add_column(
        "inbound_messages",
        sa.Column("match_method", sa.String(), server_default="none", nullable=False),
    )
    op.add_column(
        "inbound_messages",
        sa.Column("is_auto_reply", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("inbound_messages", sa.Column("classified_text", sa.Text(), nullable=True))
    op.add_column("inbound_messages", sa.Column("references_header", sa.Text(), nullable=True))
    op.add_column("inbound_messages", sa.Column("intent", sa.String(), nullable=True))
    op.add_column("inbound_messages", sa.Column("intent_confidence", sa.Float(), nullable=True))
    op.add_column(
        "inbound_messages",
        sa.Column("intent_probabilities", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("inbound_messages", sa.Column("classifier_model", sa.String(), nullable=True))
    op.add_column("inbound_messages", sa.Column("promised_date", sa.Date(), nullable=True))
    op.add_column(
        "inbound_messages",
        sa.Column("promised_date_raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("inbound_messages", sa.Column("route", sa.String(), nullable=True))
    op.add_column(
        "inbound_messages",
        sa.Column("review_status", sa.String(), server_default="not_needed", nullable=False),
    )
    op.add_column("inbound_messages", sa.Column("reviewed_by", sa.String(), nullable=True))
    op.add_column(
        "inbound_messages", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("inbound_messages", sa.Column("reviewer_intent", sa.String(), nullable=True))
    op.add_column("inbound_messages", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column(
        "inbound_messages",
        sa.Column("attachment_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.create_check_constraint(
        "match_method_known",
        "inbound_messages",
        sa.text(_in("match_method", ("reply_token", "in_reply_to", "sender", "none"))),
    )
    op.create_check_constraint(
        "review_status_known",
        "inbound_messages",
        sa.text(_in("review_status", ("not_needed", "needs_review", "reviewed"))),
    )
    op.create_check_constraint(
        "intent_known", "inbound_messages", sa.text("intent IS NULL OR " + _in("intent", _INTENTS))
    )
    op.create_check_constraint(
        "reviewer_intent_known",
        "inbound_messages",
        sa.text("reviewer_intent IS NULL OR " + _in("reviewer_intent", _INTENTS)),
    )
    op.create_index("ix_inbound_messages_review_status", "inbound_messages", ["review_status"])


def downgrade() -> None:
    """Downgrade schema.

    Refuses if reply-kind outbox rows exist: those record what customers were sent, so they are
    never deleted silently. Classification and review columns on inbound_messages are dropped.
    """
    reply_rows = (
        op.get_bind()
        .execute(sa.text("SELECT count(*) FROM outbound_emails WHERE " + _in("kind", _REPLY_KINDS)))
        .scalar_one()
    )
    if reply_rows:
        raise RuntimeError(
            f"refusing to downgrade: {reply_rows} reply-kind outbox row(s) exist; "
            "they record what customers were sent"
        )
    op.drop_index("ix_inbound_messages_review_status", table_name="inbound_messages")
    for name in (
        "reviewer_intent_known",
        "intent_known",
        "review_status_known",
        "match_method_known",
    ):
        op.drop_constraint(name, "inbound_messages", type_="check")
    for column in (
        "attachment_count",
        "review_note",
        "reviewer_intent",
        "reviewed_at",
        "reviewed_by",
        "review_status",
        "route",
        "promised_date_raw",
        "promised_date",
        "classifier_model",
        "intent_probabilities",
        "intent_confidence",
        "intent",
        "references_header",
        "classified_text",
        "is_auto_reply",
        "match_method",
        "postmark_message_id",
    ):
        op.drop_column("inbound_messages", column)

    op.drop_index("ix_outbound_emails_inbound_message_id", table_name="outbound_emails")
    op.drop_constraint(
        "outbound_emails_inbound_message_id_fkey", "outbound_emails", type_="foreignkey"
    )
    op.drop_column("outbound_emails", "inbound_message_id")
    op.drop_column("outbound_emails", "references")
    op.drop_column("outbound_emails", "in_reply_to_message_id")
    op.drop_constraint("kind_known", "outbound_emails", type_="check")
    op.create_check_constraint("kind_known", "outbound_emails", sa.text(_in("kind", _OLD_KINDS)))
