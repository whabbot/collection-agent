"""The reply routing table. Jev supplies a typed classification; everything that follows is
decided here, in code.

The only effects this module may have on an invoice are:
  * `status` -> `paid`, and only after Stripe says it is paid;
  * `state` -> `disputed` or `with_human`;
  * `promised_date` / `paused_until`, and only for a date inside the policy limits.
It never touches `amount_minor`, `currency` or `due_on`, and every email it drafts is a
`pending_approval` outbox row from a fixed template. Customer text is never copied into a draft
(except the subject line, which is cleaned and capped).
"""

import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from collection_agent.calendar import WorkingCalendar
from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.classifier import (
    AdminSubtype,
    Classification,
    Intent,
    assemble_promised_date,
)
from collection_agent.db.models import (
    REPLY_OUTBOUND_KINDS,
    Contract,
    Customer,
    InboundMessage,
    Invoice,
    OutboundEmail,
    OutboundKind,
)
from collection_agent.deps import EmailIdentity
from collection_agent.outbox import new_outbound_email
from collection_agent.policy import ChasePolicy
from collection_agent.stripe_gateway import StripeGateway
from collection_agent.templates import ReplyFacts, render_reply_body

logger = logging.getLogger(__name__)

LONDON = ZoneInfo("Europe/London")
MAX_REPLIES_PER_DAY = 2
MAX_SUBJECT_CHARS = 200
MAX_REFERENCE_IDS = 9

NOT_NEEDED = "not_needed"
NEEDS_REVIEW = "needs_review"

_MSG_ID_RE = re.compile(r"^<[^<>\s]+>$")


class RoutingError(Exception):
    """Routing could not complete (for example no email identity is configured). The caller
    rolls back everything routing did and sends the message to a human."""


@dataclass(frozen=True)
class RouteResult:
    route: str
    review_status: str
    draft: OutboundEmail | None = None


def reply_subject(original: str, reference: str) -> str:
    """`Re: <original subject>`. The subject is written by the customer and echoed into our
    draft, so control and line-break characters are removed and the length capped."""
    cleaned = "".join(
        " " if unicodedata.category(c)[0] == "C" or unicodedata.category(c) in ("Zl", "Zp") else c
        for c in original
    )
    cleaned = " ".join(cleaned.split())[:MAX_SUBJECT_CHARS].strip()
    if not cleaned:
        return f"Re: Invoice {reference}"
    if cleaned.lower().startswith("re:"):
        return cleaned
    return f"Re: {cleaned}"


def threading(message: InboundMessage) -> tuple[str | None, str | None]:
    """(In-Reply-To, References) header values for a reply to `message`, or (None, None) when
    the message has no usable Message-ID of its own."""
    own = f"<{message.message_id}>"
    if message.message_id.startswith("postmark:") or not _MSG_ID_RE.match(own):
        return None, None
    earlier = [t for t in (message.references_header or "").split() if _MSG_ID_RE.match(t)]
    ids = [*earlier[-MAX_REFERENCE_IDS:], own]
    return own, " ".join(ids)


def _london_day_bounds(today: date) -> tuple[datetime, datetime]:
    start = datetime.combine(today, time.min, tzinfo=LONDON)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=LONDON)
    return start, end


def replies_drafted_today(session: Session, invoice_id: uuid.UUID, today: date) -> int:
    start, end = _london_day_bounds(today)
    return (
        session.scalar(
            select(func.count())
            .select_from(OutboundEmail)
            .where(
                OutboundEmail.invoice_id == invoice_id,
                OutboundEmail.kind.in_([k.value for k in REPLY_OUTBOUND_KINDS]),
                OutboundEmail.created_at >= start,
                OutboundEmail.created_at < end,
            )
        )
        or 0
    )


def route_reply(
    session: Session,
    message: InboundMessage,
    classification: Classification,
    invoice: Invoice,
    policy: ChasePolicy | None,
    cal: WorkingCalendar,
    gateway: StripeGateway | None,
    identity: EmailIdentity | None,
    today: date,
    *,
    now: datetime,
    min_confidence: float,
) -> RouteResult:
    """Apply the routing table to a classified, matched, non-auto reply. Flushes but does not
    commit. Updates `message.route`, `review_status`, `counts_as_response` and `promised_date`."""
    result = _route(
        session,
        message,
        classification,
        invoice,
        policy,
        cal,
        gateway,
        identity,
        today,
        now,
        min_confidence,
    )
    message.route = result.route
    message.review_status = result.review_status
    session.flush()
    return result


def _route(
    session: Session,
    message: InboundMessage,
    classification: Classification,
    invoice: Invoice,
    policy: ChasePolicy | None,
    cal: WorkingCalendar,
    gateway: StripeGateway | None,
    identity: EmailIdentity | None,
    today: date,
    now: datetime,
    min_confidence: float,
) -> RouteResult:
    # 1. Not confident, or no usable intent: a human reads it. It still counts as a response.
    if classification.confidence < min_confidence:
        return RouteResult("low_confidence", NEEDS_REVIEW)
    if classification.intent is Intent.OTHER:
        return RouteResult("other", NEEDS_REVIEW)

    # Re-read the invoice under a row lock: the state we act on must be the state we change.
    inv = session.scalars(
        select(Invoice)
        .where(Invoice.id == invoice.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()

    # 2. Only an open, active invoice is touched.
    if inv.status is not InvoiceStatus.OPEN or inv.state is not InvoiceState.ACTIVE:
        return RouteResult("invoice_not_active", NEEDS_REVIEW)

    # 3. Never run away in a loop with an automatic sender.
    if replies_drafted_today(session, inv.id, today) >= MAX_REPLIES_PER_DAY:
        return RouteResult("reply_cap", NEEDS_REVIEW)

    drafter = _Drafter(session, message, inv, identity, now)

    match classification.intent:
        case Intent.ALREADY_PAID:
            return _already_paid(inv, gateway, drafter)
        case Intent.PROMISE_TO_PAY:
            return _promise(message, classification, inv, policy, cal, today, drafter)
        case Intent.REQUEST_EXTENSION:
            inv.state = InvoiceState.WITH_HUMAN
            return RouteResult(
                "extension_requested", NEEDS_REVIEW, drafter.draft(OutboundKind.REPLY_HOLDING)
            )
        case Intent.REQUEST_PAYMENT_PLAN:
            inv.state = InvoiceState.WITH_HUMAN
            return RouteResult(
                "payment_plan_requested", NEEDS_REVIEW, drafter.draft(OutboundKind.REPLY_HOLDING)
            )
        case Intent.DISPUTE:
            inv.state = InvoiceState.DISPUTED
            return RouteResult("dispute", NEEDS_REVIEW, drafter.draft(OutboundKind.REPLY_HOLDING))
        case Intent.ADMIN_REQUEST:
            return _admin(classification, inv, drafter)
        case Intent.CONTRACT_QUESTION:
            return RouteResult("contract_question", NEEDS_REVIEW)
        case Intent.OUT_OF_OFFICE:
            message.counts_as_response = False
            return RouteResult("out_of_office", NOT_NEEDED)
        case _:  # Intent.OTHER was handled above
            return RouteResult("other", NEEDS_REVIEW)


def _already_paid(inv: Invoice, gateway: StripeGateway | None, drafter: _Drafter) -> RouteResult:
    """Believe Stripe, never the customer."""
    if gateway is None:
        return RouteResult("stripe_error", NEEDS_REVIEW)
    try:
        status = gateway.get_invoice_status(inv.stripe_invoice_id)
    except stripe.StripeError as exc:
        logger.error("inbound: Stripe error for invoice %s: request_id=%s", inv.id, exc.request_id)
        return RouteResult("stripe_error", NEEDS_REVIEW)
    if status == "paid":
        inv.status = InvoiceStatus.PAID
        return RouteResult(
            "already_paid_confirmed", NOT_NEEDED, drafter.draft(OutboundKind.REPLY_PAID_THANKS)
        )
    if status == "open":
        return RouteResult(
            "already_paid_unconfirmed",
            NOT_NEEDED,
            drafter.draft(OutboundKind.REPLY_REMITTANCE_REQUEST),
        )
    # void, uncollectible, draft or anything new: not for this slice to interpret.
    return RouteResult("stripe_status_unexpected", NEEDS_REVIEW)


def _promise(
    message: InboundMessage,
    classification: Classification,
    inv: Invoice,
    policy: ChasePolicy | None,
    cal: WorkingCalendar,
    today: date,
    drafter: _Drafter,
) -> RouteResult:
    promised = assemble_promised_date(classification.promised_date_parts, today)
    message.promised_date = promised
    if promised is None:
        return RouteResult("promise_date_unclear", NEEDS_REVIEW)
    if not today <= promised <= inv.due_on:
        return RouteResult("promise_outside_policy", NEEDS_REVIEW)
    if policy is None:
        return RouteResult("no_policy", NEEDS_REVIEW)
    if not inv.hosted_invoice_url:
        return RouteResult("invoice_url_missing", NEEDS_REVIEW)
    inv.promised_date = promised
    inv.paused_until = cal.add_working_days(promised, policy.promise_grace_working_days)
    return RouteResult(
        "promise_accepted",
        NOT_NEEDED,
        drafter.draft(OutboundKind.REPLY_PROMISE_ACK, promised_date=promised),
    )


def _admin(classification: Classification, inv: Invoice, drafter: _Drafter) -> RouteResult:
    if classification.admin_subtype is AdminSubtype.RESEND:
        if not inv.hosted_invoice_url:
            return RouteResult("invoice_url_missing", NEEDS_REVIEW)
        has_contract = (
            drafter.session.scalars(
                select(Contract.id).where(Contract.invoice_id == inv.id)
            ).first()
            is not None
        )
        return RouteResult(
            "admin_resend",
            NOT_NEEDED,
            drafter.draft(OutboundKind.REPLY_RESEND, attach_contract=has_contract),
        )
    inv.state = InvoiceState.WITH_HUMAN
    return RouteResult(
        "admin_billing_change", NEEDS_REVIEW, drafter.draft(OutboundKind.REPLY_HOLDING)
    )


class _Drafter:
    """Builds reply drafts for one message from fixed templates."""

    def __init__(
        self,
        session: Session,
        message: InboundMessage,
        invoice: Invoice,
        identity: EmailIdentity | None,
        now: datetime,
    ) -> None:
        self.session = session
        self._message = message
        self._invoice = invoice
        self._identity = identity
        self._now = now

    def draft(
        self,
        kind: OutboundKind,
        *,
        promised_date: date | None = None,
        attach_contract: bool = False,
    ) -> OutboundEmail:
        if self._identity is None:
            raise RoutingError("email identity is not configured")
        invoice = self._invoice
        customer = self.session.get(Customer, invoice.customer_id)
        if customer is None:
            raise RoutingError("invoice has no customer")
        body = render_reply_body(
            kind,
            ReplyFacts(
                customer_name=customer.name,
                reference=invoice.reference,
                amount_minor=invoice.amount_minor,
                currency=invoice.currency,
                from_name=self._identity.from_name,
                company_name=self._identity.company_name,
                hosted_invoice_url=invoice.hosted_invoice_url,
                promised_date=promised_date,
                attach_contract=attach_contract,
            ),
        )
        in_reply_to, references = threading(self._message)
        email = new_outbound_email(
            invoice=invoice,
            customer=customer,
            identity=self._identity,
            kind=kind,
            subject=reply_subject(self._message.subject, invoice.reference),
            body=body,
            attach_contract=attach_contract,
            in_reply_to_message_id=in_reply_to,
            references=references,
            inbound_message_id=self._message.id,
        )
        email.created_at = self._now  # the injected clock, so the daily cap is testable
        self.session.add(email)
        self.session.flush()
        return email
