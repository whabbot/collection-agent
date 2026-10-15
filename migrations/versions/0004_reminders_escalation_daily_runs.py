"""reminder outbox kinds, escalation, resolved sends, daily_runs

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15 23:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_KINDS = "kind IN ('invoice')"
_NEW_KINDS = "kind IN ('invoice', 'reminder', 'reminder_pre_deadline', 'reminder_broken_promise')"
_ACTIVE_REMINDER = (
    "kind IN ('reminder', 'reminder_pre_deadline', 'reminder_broken_promise') "
    "AND status IN ('pending_approval', 'sending')"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint("kind_known", "outbound_emails", type_="check")
    op.create_check_constraint("kind_known", "outbound_emails", sa.text(_NEW_KINDS))
    op.create_index(
        "uq_outbound_emails_one_active_reminder",
        "outbound_emails",
        ["invoice_id"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_REMINDER),
    )
    op.add_column("outbound_emails", sa.Column("resolved_by", sa.String(), nullable=True))
    op.add_column(
        "outbound_emails", sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("invoices", sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "daily_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("run_date", sa.Date(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("counts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    """Downgrade schema.

    Fails (check constraint) if reminder-kind outbox rows exist: those are a record of what
    customers were sent, so they are never deleted silently.
    """
    op.drop_table("daily_runs")
    op.drop_column("invoices", "escalated_at")
    op.drop_column("outbound_emails", "resolved_at")
    op.drop_column("outbound_emails", "resolved_by")
    op.drop_index(
        "uq_outbound_emails_one_active_reminder",
        table_name="outbound_emails",
        postgresql_where=sa.text(_ACTIVE_REMINDER),
    )
    op.drop_constraint("kind_known", "outbound_emails", type_="check")
    op.create_check_constraint("kind_known", "outbound_emails", sa.text(_OLD_KINDS))
