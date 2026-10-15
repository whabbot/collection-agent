"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-15 19:28:32.516014

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "chase_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("no_response_days", sa.Integer(), nullable=False),
        sa.Column("clearance_day_of_month", sa.Integer(), nullable=False),
        sa.Column("clearance_month_offset", sa.Integer(), nullable=False),
        sa.Column("pre_deadline_buffer_working_days", sa.Integer(), nullable=False),
        sa.Column("max_reminders", sa.Integer(), nullable=False),
        sa.Column("promise_grace_working_days", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "approved", "superseded", name="policy_status"),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "clearance_day_of_month BETWEEN 1 AND 28", name="clearance_day_of_month_range"
        ),
        sa.CheckConstraint("clearance_month_offset > 0", name="clearance_month_offset_positive"),
        sa.CheckConstraint("max_reminders > 0", name="max_reminders_positive"),
        sa.CheckConstraint("no_response_days > 0", name="no_response_days_positive"),
        sa.CheckConstraint(
            "pre_deadline_buffer_working_days > 0", name="pre_deadline_buffer_positive"
        ),
        sa.CheckConstraint("promise_grace_working_days > 0", name="promise_grace_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index(
        "uq_chase_policies_one_approved",
        "chase_policies",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'approved'"),
    )
    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("stripe_customer_id", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stripe_customer_id"),
    )
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("stripe_invoice_id", sa.String(), nullable=False),
        sa.Column("amount_minor", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("hosted_invoice_url", sa.String(), nullable=True),
        sa.Column("contract_id", sa.String(), nullable=True),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.Column("status", sa.Enum("open", "paid", "void", name="invoice_status"), nullable=False),
        sa.Column(
            "state",
            sa.Enum("active", "disputed", "with_human", "escalated", name="invoice_state"),
            nullable=False,
        ),
        sa.Column("paused_until", sa.Date(), nullable=True),
        sa.Column("promised_date", sa.Date(), nullable=True),
        sa.Column("reply_token", sa.String(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("amount_minor > 0", name="amount_minor_positive"),
        sa.CheckConstraint("char_length(currency) = 3", name="currency_len_3"),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reply_token"),
        sa.UniqueConstraint("stripe_invoice_id"),
    )
    op.create_index("ix_invoices_status_state", "invoices", ["status", "state"], unique=False)
    op.create_table(
        "inbound_messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("message_id", sa.String(), nullable=False),
        sa.Column("from_address", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("counts_as_response", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id"),
    )
    op.create_index(
        "ix_inbound_messages_invoice_id", "inbound_messages", ["invoice_id"], unique=False
    )
    op.create_table(
        "reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("reminder", "pre_deadline", "broken_promise", name="reminder_kind"),
            nullable=False,
        ),
        sa.Column("sent_on", sa.Date(), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminders_invoice_id", "reminders", ["invoice_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_reminders_invoice_id", table_name="reminders")
    op.drop_table("reminders")
    op.drop_index("ix_inbound_messages_invoice_id", table_name="inbound_messages")
    op.drop_table("inbound_messages")
    op.drop_index("ix_invoices_status_state", table_name="invoices")
    op.drop_table("invoices")
    op.drop_table("customers")
    op.drop_index(
        "uq_chase_policies_one_approved",
        table_name="chase_policies",
        postgresql_where=sa.text("status = 'approved'"),
    )
    op.drop_table("chase_policies")
    # Enum types are not dropped with their tables.
    for name in ("reminder_kind", "invoice_state", "invoice_status", "policy_status"):
        op.execute(f"DROP TYPE {name}")
