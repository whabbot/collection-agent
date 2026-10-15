"""Shared setup for tests of the daily run, the reminder outbox and the CLI."""

from datetime import date

from sqlalchemy.orm import Session

from collection_agent.db.models import Invoice
from collection_agent.db.repository import approve_policy, create_draft_policy
from collection_agent.deps import EmailIdentity
from collection_agent.policy import ChasePolicy
from tests.db_helpers import make_invoice

URL = "https://invoice.stripe.test/i/in_fake1"
IDENTITY = EmailIdentity(
    "billing@billing.example.test", "Acme Billing", "billing.example.test", "Acme Co"
)
THU_8_OCT = date(2026, 10, 8)  # 7 days after the default sent_on of 1 October


def approve_default_policy(session: Session) -> None:
    version = create_draft_policy(session, ChasePolicy())
    approve_policy(session, version, "test")


def chased_invoice(session: Session, **kw: object) -> Invoice:
    """An open, sent invoice with a payment link (sent 2026-10-01, due 2026-11-16 by default)."""
    inv = make_invoice(session, **kw)  # type: ignore[arg-type]
    inv.hosted_invoice_url = URL
    session.flush()
    return inv
