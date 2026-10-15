"""POST /invoices: create a Stripe customer and finalized invoice, then store both."""

import logging
import uuid
from datetime import date, datetime, time
from typing import Annotated, Literal

import stripe
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from collection_agent.calendar import uk_calendar
from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Customer, Invoice, Reminder
from collection_agent.db.repository import NoApprovedPolicyError, get_approved_policy
from collection_agent.deps import LONDON, AdminDep, GatewayDep, SessionDep, TodayDep
from collection_agent.stripe_gateway import StripeGateway

logger = logging.getLogger(__name__)

router = APIRouter()

# Stripe caps a single amount at 99,999,999 minor units; keeping the total under it also keeps
# it inside the Integer column.
MAX_TOTAL_MINOR = 99_999_999


class CustomerIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
    email: EmailStr


class LineIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    amount_minor: Annotated[int, Field(gt=0, le=MAX_TOTAL_MINOR)]


class InvoiceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    customer: CustomerIn
    currency: Annotated[str, StringConstraints(pattern=r"^[a-z]{3}$")]
    lines: Annotated[list[LineIn], Field(min_length=1, max_length=50)]
    contract_id: Annotated[str, StringConstraints(max_length=64)] | None = None

    @model_validator(mode="after")
    def _total_in_range(self) -> InvoiceIn:
        if self.total_minor > MAX_TOTAL_MINOR:
            raise ValueError(f"total of lines exceeds {MAX_TOTAL_MINOR}")
        return self

    @property
    def total_minor(self) -> int:
        return sum(line.amount_minor for line in self.lines)


class InvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    stripe_invoice_id: str
    hosted_invoice_url: str | None
    amount_minor: int
    currency: str
    due_on: date
    status: str


def _out(inv: Invoice) -> InvoiceOut:
    return InvoiceOut(
        id=inv.id,
        reference=inv.reference,
        stripe_invoice_id=inv.stripe_invoice_id,
        hosted_invoice_url=inv.hosted_invoice_url,
        amount_minor=inv.amount_minor,
        currency=inv.currency,
        due_on=inv.due_on,
        status=inv.status.value,
    )


def due_timestamp(due_on: date) -> int:
    """Unix timestamp of 23:59:59 Europe/London on `due_on`."""
    return int(datetime.combine(due_on, time(23, 59, 59), tzinfo=LONDON).timestamp())


def _existing_response(session: Session, body: InvoiceIn) -> JSONResponse | None:
    """200 with the stored invoice if it matches the request, 409 if not, None if absent."""
    row = session.execute(
        select(Invoice, Customer.email)
        .join(Customer, Customer.id == Invoice.customer_id)
        .where(Invoice.reference == body.reference)
    ).one_or_none()
    if row is None:
        return None
    inv, email = row
    matches = (
        email.lower() == body.customer.email.lower()
        and inv.currency.lower() == body.currency
        and inv.amount_minor == body.total_minor
    )
    if not matches:
        return JSONResponse({"error": "reference_conflict"}, status_code=409)
    return JSONResponse(_out(inv).model_dump(mode="json"), status_code=200)


def _find_customer(session: Session, email: str) -> Customer | None:
    """Our customer row for this email, matched case-insensitively."""
    return session.scalars(
        select(Customer)
        .where(func.lower(Customer.email) == email.lower())
        .order_by(Customer.created_at, Customer.id)
        .limit(1)
    ).first()


def _store_invoice(
    session: Session,
    *,
    body: InvoiceIn,
    customer: Customer | None,
    stripe_customer_id: str,
    stripe_invoice_id: str,
    hosted_invoice_url: str | None,
    due_on: date,
) -> Invoice:
    """Insert the customer (if new) and the invoice, in one transaction. Does not commit."""
    if customer is None:
        customer = Customer(
            stripe_customer_id=stripe_customer_id,
            name=body.customer.name,
            email=body.customer.email,
        )
        session.add(customer)
        session.flush()
    invoice = Invoice(
        customer_id=customer.id,
        stripe_invoice_id=stripe_invoice_id,
        reference=body.reference,
        amount_minor=body.total_minor,
        currency=body.currency,
        hosted_invoice_url=hosted_invoice_url,
        contract_id=body.contract_id,
        sent_on=None,
        due_on=due_on,
        status=InvoiceStatus.OPEN,
        state=InvoiceState.ACTIVE,
    )
    session.add(invoice)
    session.flush()
    return invoice


def _create_in_stripe(
    gateway: StripeGateway, customer: Customer | None, body: InvoiceIn, due_on: date
) -> tuple[str, str, str | None]:
    """Run the Stripe calls. Returns (customer id, invoice id, hosted url)."""
    ref = body.reference
    customer_id = (
        customer.stripe_customer_id
        if customer is not None
        else gateway.create_customer(body.customer.name, body.customer.email, f"{ref}:customer")
    )
    metadata = {"reference": ref}
    if body.contract_id is not None:
        metadata["contract_id"] = body.contract_id
    invoice_id = gateway.create_invoice(
        customer_id, body.currency, due_timestamp(due_on), metadata, f"{ref}:invoice"
    )
    for i, line in enumerate(body.lines):
        gateway.add_invoice_item(
            invoice_id,
            customer_id,
            line.amount_minor,
            body.currency,
            line.description,
            f"{ref}:line:{i}",
        )
    finalized = gateway.finalize_invoice(invoice_id, f"{ref}:finalize")
    return customer_id, invoice_id, finalized.hosted_invoice_url


@router.post("/invoices", response_model=None, status_code=201)
def create_invoice(
    body: InvoiceIn,
    _admin: AdminDep,
    gateway: GatewayDep,
    session: SessionDep,
    today: TodayDep,
) -> Response:
    if (existing := _existing_response(session, body)) is not None:
        return existing

    try:
        policy = get_approved_policy(session)
    except NoApprovedPolicyError:
        return JSONResponse({"error": "no_approved_policy"}, status_code=409)

    due_on = policy.clearance_deadline(today, uk_calendar([today.year, today.year + 1]))

    customer = _find_customer(session, body.customer.email)
    try:
        customer_id, stripe_invoice_id, url = _create_in_stripe(gateway, customer, body, due_on)
    except stripe.StripeError as exc:
        # Never log the message (it can echo part of a key) or the key itself.
        logger.error(
            "stripe error on create invoice: type=%s request_id=%s http_status=%s code=%s",
            type(exc).__name__,
            exc.request_id,
            exc.http_status,
            exc.code,
        )
        session.rollback()
        return JSONResponse({"error": "stripe_error"}, status_code=502)

    try:
        invoice = _store_invoice(
            session,
            body=body,
            customer=customer,
            stripe_customer_id=customer_id,
            stripe_invoice_id=stripe_invoice_id,
            hosted_invoice_url=url,
            due_on=due_on,
        )
        session.commit()
    except IntegrityError:
        # A concurrent request with the same reference got there first.
        session.rollback()
        if (existing := _existing_response(session, body)) is not None:
            return existing
        raise
    return JSONResponse(_out(invoice).model_dump(mode="json"), status_code=201)


class EscalatedInvoiceOut(BaseModel):
    id: uuid.UUID
    reference: str
    customer_name: str
    customer_email: str
    amount_minor: int
    currency: str
    due_on: date
    escalated_at: datetime | None
    reminder_count: int


@router.get("/invoices", response_model=None)
def list_invoices(
    _admin: AdminDep, session: SessionDep, state: Literal["escalated"]
) -> JSONResponse:
    """Invoices the daily run handed to a human, newest escalation first. Only the `state`
    filter exists, and only for `escalated`."""
    reminder_count = (
        select(func.count(Reminder.id))
        .where(Reminder.invoice_id == Invoice.id)
        .correlate(Invoice)
        .scalar_subquery()
    )
    rows = session.execute(
        select(Invoice, Customer, reminder_count)
        .join(Customer, Customer.id == Invoice.customer_id)
        .where(Invoice.state == InvoiceState.ESCALATED)
        .order_by(Invoice.escalated_at.desc().nulls_last(), Invoice.id)
    ).all()
    return JSONResponse(
        [
            EscalatedInvoiceOut(
                id=inv.id,
                reference=inv.reference,
                customer_name=cust.name,
                customer_email=cust.email,
                amount_minor=inv.amount_minor,
                currency=inv.currency,
                due_on=inv.due_on,
                escalated_at=inv.escalated_at,
                reminder_count=count,
            ).model_dump(mode="json")
            for inv, cust, count in rows
        ]
    )
