"""contracts and the outbound_emails outbox

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-15 23:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "contracts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("invoice_id"),
    )
    op.create_table(
        "outbound_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("invoice_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("to_address", sa.String(), nullable=False),
        sa.Column("from_address", sa.String(), nullable=False),
        sa.Column("from_name", sa.String(), nullable=False),
        sa.Column("reply_to", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("attach_contract", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending_approval",
                "sending",
                "sent",
                "rejected",
                "failed",
                name="outbound_status",
            ),
            nullable=False,
        ),
        sa.Column("approved_by", sa.String(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.String(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("kind IN ('invoice')", name="kind_known"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_outbound_emails_status", "outbound_emails", ["status"])
    op.create_index(
        "uq_outbound_emails_one_live_invoice_email",
        "outbound_emails",
        ["invoice_id"],
        unique=True,
        postgresql_where=sa.text("kind = 'invoice' AND status NOT IN ('rejected', 'failed')"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "uq_outbound_emails_one_live_invoice_email",
        table_name="outbound_emails",
        postgresql_where=sa.text("kind = 'invoice' AND status NOT IN ('rejected', 'failed')"),
    )
    op.drop_index("ix_outbound_emails_status", table_name="outbound_emails")
    op.drop_table("outbound_emails")
    op.drop_table("contracts")
    # Enum types are not dropped with their tables.
    op.execute("DROP TYPE outbound_status")
