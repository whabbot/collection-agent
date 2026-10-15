"""Plain-text email templates. Plain string formatting, no template engine, no locale."""

from datetime import date
from decimal import Decimal

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
