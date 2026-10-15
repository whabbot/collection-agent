import uuid
from datetime import date

from sqlalchemy.orm import Session

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Customer, Invoice


def make_customer(session: Session) -> Customer:
    cust = Customer(
        stripe_customer_id=f"cus_{uuid.uuid4().hex[:12]}", name="Acme Ltd", email="ap@acme.test"
    )
    session.add(cust)
    session.flush()
    return cust


def make_invoice(
    session: Session,
    *,
    sent_on: date = date(2026, 10, 1),
    status: InvoiceStatus = InvoiceStatus.OPEN,
    state: InvoiceState = InvoiceState.ACTIVE,
    stripe_invoice_id: str | None = None,
    amount_minor: int = 12_000,
    customer: Customer | None = None,
    paused_until: date | None = None,
    promised_date: date | None = None,
) -> Invoice:
    cust = customer or make_customer(session)
    inv = Invoice(
        customer_id=cust.id,
        stripe_invoice_id=stripe_invoice_id or f"in_{uuid.uuid4().hex[:12]}",
        amount_minor=amount_minor,
        currency="GBP",
        sent_on=sent_on,
        status=status,
        state=state,
        paused_until=paused_until,
        promised_date=promised_date,
    )
    session.add(inv)
    session.flush()
    return inv
