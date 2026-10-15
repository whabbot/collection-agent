"""policy documents and Claude extractions

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-15 23:59:40.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("cognee_status", sa.String(), server_default="not_configured", nullable=False),
        sa.Column("cognee_error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "cognee_status IN ('not_configured', 'ingested', 'failed')",
            name="policy_documents_cognee_status",
        ),
    )
    op.create_table(
        "policy_extractions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("policy_document_id", sa.Uuid(), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("model", sa.String(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("soft_guidance", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["policy_document_id"], ["policy_documents.id"]),
        sa.ForeignKeyConstraint(["policy_version"], ["chase_policies.version"]),
        sa.UniqueConstraint("policy_version"),
    )


def downgrade() -> None:
    op.drop_table("policy_extractions")
    op.drop_table("policy_documents")
