"""demo helpers: the simulated clock and the log email sender's record

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-16 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "demo_clock",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("today", sa.Date(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="demo_clock_single_row"),
    )
    op.create_table(
        "demo_sent_emails",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("to_address", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False),
        sa.Column("attachment_names", postgresql.JSONB(), nullable=False),
        sa.Column("provider_message_id", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("demo_sent_emails")
    op.drop_table("demo_clock")
