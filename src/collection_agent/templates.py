"""Plain-text email templates. Plain string formatting, no template engine, no locale."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from collection_agent.chasing import ReminderKind
from collection_agent.db.models import OutboundKind

_SYMBOLS = {"gbp": "£", "eur": "€", "usd": "$"}
# Stripe's zero-decimal and three-decimal currencies.
_ZERO_DECIMAL = frozenset(
    ["bif", "clp", "djf", "gnf", "jpy", "kmf", "krw", "mga"]
    + ["pyg", "rwf", "ugx", "vnd", "vuv", "xaf", "xof", "xpf"]
)
_THREE_DECIMAL = frozenset(["bhd", "jod", "kwd", "omr", "tnd"])

_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = ["January", "February", "March", "April", "May", "June"] + [
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


def format_amount(amount_minor: int, currency: str) -> str:
    code = currency.lower()
    if code in _ZERO_DECIMAL:
        places = 0
    elif code in _THREE_DECIMAL:
        places = 3
    else:
        places = 2
    value = Decimal(amount_minor).scaleb(-places)
    number = f"{value:,.{places}f}"
    if places == 2 and code in _SYMBOLS:
        return f"{_SYMBOLS[code]}{number}"
    return f"{number} {code.upper()}"


def format_date(day: date) -> str:
    return f"{_WEEKDAYS[day.weekday()]} {day.day} {_MONTHS[day.month - 1]} {day.year}"


def render_invoice_email(
    *,
    reference: str,
    customer_name: str,
    amount_minor: int,
    currency: str,
    due_on: date,
    hosted_invoice_url: str | None,
    contract_id: str | None,
    from_name: str,
    company_name: str,
) -> tuple[str, str]:
    """Return (subject, body). Refuses to render without a payment link."""
    if not hosted_invoice_url:
        raise ValueError("cannot render an invoice email without a hosted_invoice_url")
    contract_ref = f" ({contract_id})" if contract_id else ""
    subject = f"Invoice {reference} from {company_name}"
    body = (
        f"Hello {customer_name},\n"
        "\n"
        f"Please find attached your contract{contract_ref}. Invoice {reference} for "
        f"{format_amount(amount_minor, currency)} is due by {format_date(due_on)}.\n"
        "\n"
        "You can view and pay it securely here:\n"
        f"{hosted_invoice_url}\n"
        "\n"
        "If you have any questions, just reply to this email.\n"
        "\n"
        "Thank you,\n"
        f"{from_name}\n"
        f"{company_name}\n"
    )
    return subject, body


@dataclass(frozen=True)
class ReminderFacts:
    customer_name: str
    reference: str
    amount_minor: int
    currency: str
    due_on: date
    today: date
    hosted_invoice_url: str
    from_name: str
    company_name: str
    promised_date: date | None = None  # required for BROKEN_PROMISE only


def _opening(kind: ReminderKind, facts: ReminderFacts) -> str:
    amount = format_amount(facts.amount_minor, facts.currency)
    due_date = format_date(facts.due_on)
    if kind is ReminderKind.PRE_DEADLINE:
        return (
            f"This is a reminder that invoice {facts.reference} for {amount} is due by {due_date}."
        )
    # Absolute dates only: a draft can wait days for approval, so "in 3 days" would go stale.
    if facts.today <= facts.due_on:
        due_phrase = f"is due by {due_date}"
    else:
        due_phrase = f"was due on {due_date} and is now overdue"
    if kind is ReminderKind.BROKEN_PROMISE:
        if facts.promised_date is None:
            raise ValueError("a broken-promise reminder needs promised_date")
        return (
            f"You let us know you expected to pay invoice {facts.reference} for {amount} "
            f"by {format_date(facts.promised_date)}, but we haven't yet received payment. "
            f"It {due_phrase}."
        )
    return (
        f"Invoice {facts.reference} for {amount} {due_phrase}, and we haven't yet received payment."
    )


def render_reminder_email(kind: ReminderKind, level: int, facts: ReminderFacts) -> tuple[str, str]:
    """Return (subject, body) for a chase reminder. `level` (reminders sent + 1) is clamped
    to 1-3: friendly, firm, formal. Refuses to render without a payment link."""
    if not facts.hosted_invoice_url:
        raise ValueError("cannot render a reminder without a hosted_invoice_url")
    level = min(max(level, 1), 3)
    opening = _opening(kind, facts)
    url = facts.hosted_invoice_url
    signoff = f"{facts.from_name}\n{facts.company_name}\n"
    if level == 1:
        subject = f"Reminder: invoice {facts.reference} from {facts.company_name}"
        body = (
            f"Hello {facts.customer_name},\n\n"
            f"{opening}\n\n"
            f"You can view and pay it here:\n{url}\n\n"
            "If you've already paid, thank you, and please ignore this email. "
            "If anything is unclear, just reply.\n\n"
            f"Thanks,\n{signoff}"
        )
    elif level == 2:
        subject = f"Second reminder: invoice {facts.reference}"
        body = (
            f"Hello {facts.customer_name},\n\n"
            f"{opening}\n\n"
            f"Please arrange payment using the link below:\n{url}\n\n"
            "If there's a problem with the invoice, please reply so we can sort it out.\n\n"
            f"Regards,\n{signoff}"
        )
    else:
        subject = f"Invoice {facts.reference}: payment required"
        body = (
            f"Dear {facts.customer_name},\n\n"
            f"{opening}\n\n"
            f"Please arrange payment promptly using the link below:\n{url}\n\n"
            "If we don't hear from you, this will be passed to our accounts team to follow up. "
            "If you believe this invoice is incorrect, please reply and tell us why.\n\n"
            f"Yours sincerely,\n{signoff}"
        )
    return subject, body


# --- replies to customer emails ---------------------------------------------------------------


@dataclass(frozen=True)
class ReplyFacts:
    """Only facts we hold, plus (for a promise) the date the customer said they would pay.
    Nothing here comes from the customer's email text."""

    customer_name: str
    reference: str
    amount_minor: int
    currency: str
    from_name: str
    company_name: str
    hosted_invoice_url: str | None = None
    promised_date: date | None = None
    attach_contract: bool = False


REPLY_KINDS = (
    OutboundKind.REPLY_PAID_THANKS,
    OutboundKind.REPLY_REMITTANCE_REQUEST,
    OutboundKind.REPLY_PROMISE_ACK,
    OutboundKind.REPLY_HOLDING,
    OutboundKind.REPLY_RESEND,
)


def render_reply_body(kind: OutboundKind, facts: ReplyFacts) -> str:
    """The plain-text body of a reply draft. Refuses to render a kind that needs a payment link
    or a promised date without one."""
    amount = format_amount(facts.amount_minor, facts.currency)
    ref = facts.reference
    hello = f"Hello {facts.customer_name},\n\n"
    url = facts.hosted_invoice_url
    match kind:
        case OutboundKind.REPLY_PAID_THANKS:
            text = (
                f"Thank you. We can confirm that invoice {ref} for {amount} has been paid. "
                "No further action is needed."
            )
        case OutboundKind.REPLY_REMITTANCE_REQUEST:
            text = (
                "Thank you for letting us know. We haven't been able to match a payment to "
                f"invoice {ref} for {amount} yet. Could you reply with the payment date and the "
                "remittance or transaction reference, so we can find it?"
            )
        case OutboundKind.REPLY_PROMISE_ACK:
            if not url or facts.promised_date is None:
                raise ValueError("a promise acknowledgement needs a payment link and a date")
            text = (
                "Thank you for letting us know. We've noted that you expect to pay invoice "
                f"{ref} for {amount} by {format_date(facts.promised_date)}. You can pay here:\n"
                f"{url}"
            )
        case OutboundKind.REPLY_HOLDING:
            text = (
                f"Thank you for your message about invoice {ref}. A member of our team will "
                "look at it and get back to you."
            )
        case OutboundKind.REPLY_RESEND:
            if not url:
                raise ValueError("a resend needs a payment link")
            text = f"As requested, you can view and pay invoice {ref} for {amount} here:\n{url}"
            if facts.attach_contract:
                text += "\n\nYour contract is attached."
        case _:
            raise ValueError(f"{kind.value} is not a reply kind")
    return f"{hello}{text}\n\nThanks,\n{facts.from_name}\n{facts.company_name}\n"
