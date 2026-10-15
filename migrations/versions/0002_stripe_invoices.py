"""invoice reference and due_on, nullable sent_on, stripe_events

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-15 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add as nullable, backfill any pre-existing rows, then tighten.
    op.add_column("invoices", sa.Column("reference", sa.String(), nullable=True))
    op.add_column("invoices", sa.Column("due_on", sa.Date(), nullable=True))
    op.execute("UPDATE invoices SET reference = 'legacy-' || id::text, due_on = sent_on")
    op.alter_column("invoices", "reference", nullable=False)
    op.alter_column("invoices", "due_on", nullable=False)
    op.create_unique_constraint("invoices_reference_key", "invoices", ["reference"])
    op.alter_column("invoices", "sent_on", existing_type=sa.Date(), nullable=True)

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("stripe_object_id", sa.String(), nullable=True),
        sa.Column("livemode", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
    )


def downgrade() -> None:
    """Downgrade schema. Assumes no unsent invoices (sent_on IS NULL) exist."""
    op.drop_table("stripe_events")
    op.alter_column("invoices", "sent_on", existing_type=sa.Date(), nullable=False)
    op.drop_constraint("invoices_reference_key", "invoices", type_="unique")
    op.drop_column("invoices", "due_on")
    op.drop_column("invoices", "reference")
