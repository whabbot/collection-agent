"""The daily run: decide what is due per open invoice, then draft a reminder or escalate.

Pure orchestration. The timing decision is `decide_next_action` and the approved `ChasePolicy`;
nothing here asks an LLM or Cognee. Reminders are only ever drafted into the approval outbox.
"""

import logging
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import stripe
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.calendar import WorkingCalendar, uk_calendar
from collection_agent.chasing import (
    Action,
    InvoiceState,
    InvoiceStatus,
    decide_next_action,
)
from collection_agent.db.models import (
    REMINDER_OUTBOUND_KINDS,
    Customer,
    DailyRun,
    Invoice,
    OutboundEmail,
    OutboundKind,
    OutboundStatus,
)
from collection_agent.db.repository import (
    get_approved_policy,
    list_open_invoice_ids,
    load_decision_inputs,
)
from collection_agent.deps import EmailIdentity
from collection_agent.outbox import new_outbound_email
from collection_agent.policy import ChasePolicy
from collection_agent.stripe_gateway import StripeGateway
from collection_agent.templates import ReminderFacts, render_reminder_email

logger = logging.getLogger(__name__)

AWAITING_APPROVAL = "awaiting_approval"
NOTHING_DUE = "nothing_due"
ESCALATED = "escalated"
STRIPE_ERROR = "stripe_error"
ERROR = "error"
PAID_IN_STRIPE = "paid_in_stripe"
VOID_IN_STRIPE = "void_in_stripe"
UNCOLLECTIBLE_IN_STRIPE = "uncollectible_in_stripe"

_ACTION_KIND: dict[Action, OutboundKind] = {
    Action.REMIND: OutboundKind.REMINDER,
    Action.REMIND_PRE_DEADLINE: OutboundKind.REMINDER_PRE_DEADLINE,
    Action.REMIND_BROKEN_PROMISE: OutboundKind.REMINDER_BROKEN_PROMISE,
}
_ACTIVE_REMINDER_STATUSES = (OutboundStatus.PENDING_APPROVAL, OutboundStatus.SENDING)
# Outcomes that describe a write which a dry run only reports.
_WRITE_OUTCOME_PREFIXES = ("drafted:", ESCALATED)
# Placeholders so a dry run can render without the email identity being configured.
_DRY_RUN_IDENTITY = EmailIdentity("dry-run@invalid", "[sender]", "invalid", "[company]")


class DailyRunError(RuntimeError):
    pass


@dataclass(frozen=True)
class InvoiceOutcome:
    invoice_id: uuid.UUID
    reference: str
    outcome: str


@dataclass
class DailyReport:
    run_date: date
    invoices: list[InvoiceOutcome] = field(default_factory=list)
    dry_run: bool = False

    @property
    def counts(self) -> dict[str, int]:
        return dict(sorted(Counter(i.outcome for i in self.invoices).items()))

    @property
    def has_errors(self) -> bool:
        return any(i.outcome.removeprefix("would_") in (STRIPE_ERROR, ERROR) for i in self.invoices)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_date": self.run_date.isoformat(),
            "dry_run": self.dry_run,
            "counts": self.counts,
            "invoices": [
                {"invoice_id": str(i.invoice_id), "reference": i.reference, "outcome": i.outcome}
                for i in self.invoices
            ],
        }


def run_daily(
    session_factory: sessionmaker[Session],
    gateway: StripeGateway | None,
    identity: EmailIdentity | None,
    today: date,
    *,
    dry_run: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DailyReport:
    """Run one pass. Raises `NoApprovedPolicyError` if no policy is approved (never defaults)."""
    if not dry_run and (gateway is None or identity is None):
        raise DailyRunError("a live run needs a Stripe gateway and an email identity")
    started_at = now()
    with session_factory() as session:
        policy = get_approved_policy(session)
        invoice_ids = list_open_invoice_ids(session)
        session.rollback()  # reads only; end the transaction before the per-invoice ones

    report = DailyReport(run_date=today, dry_run=dry_run)
    cal = uk_calendar([today.year - 1, today.year, today.year + 1])
    if not cal.is_working_day(today):
        return report

    for invoice_id in invoice_ids:
        outcome, reference = _process_invoice(
            session_factory,
            gateway,
            identity or _DRY_RUN_IDENTITY,
            policy,
            cal,
            today,
            invoice_id,
            dry_run,
        )
        report.invoices.append(InvoiceOutcome(invoice_id, reference, outcome))

    if not dry_run:
        with session_factory() as session:
            session.add(
                DailyRun(
                    run_date=today,
                    started_at=started_at,
                    finished_at=now(),
                    dry_run=False,
                    counts=report.counts,
                )
            )
            session.commit()
    return report


def _process_invoice(
    session_factory: sessionmaker[Session],
    gateway: StripeGateway | None,
    identity: EmailIdentity,
    policy: ChasePolicy,
    cal: WorkingCalendar,
    today: date,
    invoice_id: uuid.UUID,
    dry_run: bool,
) -> tuple[str, str]:
    """One invoice in its own transaction; any failure here leaves the others unaffected."""
    reference = str(invoice_id)
    with session_factory() as session:
        try:
            # Lock the invoice (like approve does) so a concurrent approval or run serialises
            # with this one. A dry run takes no locks and writes nothing.
            stmt = select(Invoice).where(Invoice.id == invoice_id)
            if not dry_run:
                stmt = stmt.with_for_update()
            invoice = session.scalars(stmt.execution_options(populate_existing=True)).one()
            reference = invoice.reference
            outcome = _decide_and_apply(
                session, gateway, identity, policy, cal, today, invoice, dry_run
            )
            if dry_run:
                session.rollback()
                if outcome.startswith(_WRITE_OUTCOME_PREFIXES):
                    outcome = f"would_{outcome}"
            else:
                session.commit()
            return outcome, reference
        except IntegrityError:
            # Lost the race on the active-reminder index: someone else drafted it first.
            session.rollback()
            return AWAITING_APPROVAL, reference
        except Exception as exc:
            session.rollback()
            # Type only: exception text could carry customer data.
            logger.error("daily run: invoice %s failed: %s", invoice_id, type(exc).__name__)
            return ERROR, reference


def _has_active_reminder(session: Session, invoice_id: uuid.UUID) -> bool:
    return (
        session.scalars(
            select(OutboundEmail.id).where(
                OutboundEmail.invoice_id == invoice_id,
                OutboundEmail.kind.in_([k.value for k in REMINDER_OUTBOUND_KINDS]),
                OutboundEmail.status.in_(_ACTIVE_REMINDER_STATUSES),
            )
        ).first()
        is not None
    )


def _decide_and_apply(
    session: Session,
    gateway: StripeGateway | None,
    identity: EmailIdentity,
    policy: ChasePolicy,
    cal: WorkingCalendar,
    today: date,
    invoice: Invoice,
    dry_run: bool,
) -> str:
    # The list was read earlier; the invoice may since have been paid, paused or escalated.
    if (
        invoice.status is not InvoiceStatus.OPEN
        or invoice.state is not InvoiceState.ACTIVE
        or invoice.sent_on is None
    ):
        return NOTHING_DUE
    if _has_active_reminder(session, invoice.id):
        return AWAITING_APPROVAL

    inputs = load_decision_inputs(session, invoice.id)
    action = decide_next_action(
        inputs.invoice, policy, inputs.reminders, inputs.replies, today, cal
    )
    if action is None:
        return NOTHING_DUE

    if not dry_run:
        assert gateway is not None
        stripe_outcome = _recheck_stripe(gateway, invoice)
        if stripe_outcome is not None:
            return stripe_outcome

    if action is Action.ESCALATE_TO_HUMAN:
        if not dry_run:
            invoice.state = InvoiceState.ESCALATED
            invoice.escalated_at = datetime.now(UTC)
            session.flush()
        return ESCALATED

    kind = _ACTION_KIND[action]
    customer = session.get(Customer, invoice.customer_id)
    assert customer is not None
    if not invoice.hosted_invoice_url:
        raise DailyRunError("invoice has no hosted_invoice_url")
    facts = ReminderFacts(
        customer_name=customer.name,
        reference=invoice.reference,
        amount_minor=invoice.amount_minor,
        currency=invoice.currency,
        due_on=invoice.due_on,
        today=today,
        hosted_invoice_url=invoice.hosted_invoice_url,
        from_name=identity.from_name,
        company_name=identity.company_name,
        promised_date=invoice.promised_date,
    )
    level = len(inputs.reminders) + 1
    subject, body = render_reminder_email(REMINDER_OUTBOUND_KINDS[kind], level, facts)
    if not dry_run:
        session.add(
            new_outbound_email(
                invoice=invoice,
                customer=customer,
                identity=identity,
                kind=kind,
                subject=subject,
                body=body,
                attach_contract=False,
            )
        )
        session.flush()  # surfaces a unique-index race here, inside the try
    return f"drafted:{kind.value}"


def _recheck_stripe(gateway: StripeGateway, invoice: Invoice) -> str | None:
    """Confirm the invoice is still open in Stripe. None means carry on; anything else is the
    outcome to return (and any needed state change has been applied to `invoice`)."""
    try:
        status = gateway.get_invoice_status(invoice.stripe_invoice_id)
    except stripe.StripeError as exc:
        logger.error(
            "daily run: Stripe error for invoice %s: request_id=%s", invoice.id, exc.request_id
        )
        return STRIPE_ERROR
    match status:
        case "open":
            return None
        case "paid":
            invoice.status = InvoiceStatus.PAID
            return PAID_IN_STRIPE
        case "void":
            invoice.status = InvoiceStatus.VOID
            return VOID_IN_STRIPE
        case "uncollectible":
            invoice.state = InvoiceState.WITH_HUMAN
            return UNCOLLECTIBLE_IN_STRIPE
        case _:
            logger.error("daily run: invoice %s has unexpected Stripe status", invoice.id)
            return STRIPE_ERROR
