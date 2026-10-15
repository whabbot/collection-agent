from datetime import date

import pytest

from collection_agent.templates import format_amount, format_date, render_invoice_email

URL = "https://invoice.stripe.test/i/in_fake1"


def render(**over: object) -> tuple[str, str]:
    args: dict[str, object] = {
        "reference": "INV-2026-0042",
        "customer_name": "Acme Ltd",
        "amount_minor": 255000,
        "currency": "gbp",
        "due_on": date(2026, 11, 16),
        "hosted_invoice_url": URL,
        "contract_id": "CTR-17",
        "from_name": "Acme Billing",
        "company_name": "Acme Co",
    }
    return render_invoice_email(**{**args, **over})  # type: ignore[arg-type]


def test_gbp_with_contract_id_exact_text() -> None:
    subject, body = render()
    assert subject == "Invoice INV-2026-0042 from Acme Co"
    assert body == (
        "Hello Acme Ltd,\n"
        "\n"
        "Please find attached your contract (CTR-17). Invoice INV-2026-0042 for £2,550.00 "
        "is due by Monday 16 November 2026.\n"
        "\n"
        "You can view and pay it securely here:\n"
        "https://invoice.stripe.test/i/in_fake1\n"
        "\n"
        "If you have any questions, just reply to this email.\n"
        "\n"
        "Thank you,\n"
        "Acme Billing\n"
        "Acme Co\n"
    )


def test_no_contract_id() -> None:
    _, body = render(contract_id=None)
    assert "Please find attached your contract. Invoice" in body


def test_zero_decimal_currency() -> None:
    assert render(currency="jpy", amount_minor=250000)[1].count("250,000 JPY") == 1


def test_non_symbol_currency() -> None:
    assert "2,500.00 CHF" in render(currency="chf", amount_minor=250000)[1]


def test_missing_url_raises() -> None:
    with pytest.raises(ValueError):
        render(hosted_invoice_url=None)
    with pytest.raises(ValueError):
        render(hosted_invoice_url="")


@pytest.mark.parametrize(
    ("minor", "currency", "expected"),
    [
        (5, "gbp", "£0.05"),
        (100_000_000, "eur", "€1,000,000.00"),
        (1999, "USD", "$19.99"),
        (1500, "krw", "1,500 KRW"),
        (1500, "vnd", "1,500 VND"),
        (1234567, "kwd", "1,234.567 KWD"),
        (10, "cad", "0.10 CAD"),
    ],
)
def test_format_amount(minor: int, currency: str, expected: str) -> None:
    assert format_amount(minor, currency) == expected


@pytest.mark.parametrize(
    ("day", "expected"),
    [
        (date(2026, 11, 16), "Monday 16 November 2026"),
        (date(2027, 1, 1), "Friday 1 January 2027"),
        (date(2026, 12, 6), "Sunday 6 December 2026"),
    ],
)
def test_format_date(day: date, expected: str) -> None:
    assert format_date(day) == expected
