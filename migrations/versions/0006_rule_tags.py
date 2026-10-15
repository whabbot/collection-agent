"""rule tags: which rule fired on a reminder draft, a sent reminder or an escalation

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-15 23:59:30.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("outbound_emails", "rule_code"),
    ("outbound_emails", "rule_label"),
    ("reminders", "rule_code"),
    ("reminders", "rule_label"),
    ("invoices", "escalation_rule_code"),
    ("invoices", "escalation_rule_label"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.add_column(table, sa.Column(column, sa.String(), nullable=True))


def downgrade() -> None:
    for table, column in reversed(_COLUMNS):
        op.drop_column(table, column)
