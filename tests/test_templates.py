from datetime import date

import pytest

from collection_agent.chasing import ReminderKind
from collection_agent.templates import (
    ReminderFacts,
    format_amount,
    format_date,
    render_invoice_email,
    render_reminder_email,
)

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


# --- reminders ------------------------------------------------------------------------------

BEFORE_DUE = date(2026, 11, 2)  # due_on is Monday 16 November 2026
AFTER_DUE = date(2026, 11, 17)


def facts(today: date = BEFORE_DUE, **over: object) -> ReminderFacts:
    args: dict[str, object] = {
        "customer_name": "Acme Ltd",
        "reference": "INV-1",
        "amount_minor": 255000,
        "currency": "gbp",
        "due_on": date(2026, 11, 16),
        "today": today,
        "hosted_invoice_url": URL,
        "from_name": "Acme Billing",
        "company_name": "Acme Co",
        "promised_date": date(2026, 11, 9),
    }
    return ReminderFacts(**{**args, **over})  # type: ignore[arg-type]


OPENING = {
    (ReminderKind.REMINDER, "before"): (
        "Invoice INV-1 for £2,550.00 is due by Monday 16 November 2026, "
        "and we haven't yet received payment."
    ),
    (ReminderKind.REMINDER, "after"): (
        "Invoice INV-1 for £2,550.00 was due on Monday 16 November 2026 and is now overdue, "
        "and we haven't yet received payment."
    ),
    (ReminderKind.PRE_DEADLINE, "before"): (
        "This is a reminder that invoice INV-1 for £2,550.00 is due by Monday 16 November 2026."
    ),
    (ReminderKind.BROKEN_PROMISE, "before"): (
        "You let us know you expected to pay invoice INV-1 for £2,550.00 by Monday 9 November "
        "2026, but we haven't yet received payment. It is due by Monday 16 November 2026."
    ),
    (ReminderKind.BROKEN_PROMISE, "after"): (
        "You let us know you expected to pay invoice INV-1 for £2,550.00 by Monday 9 November "
        "2026, but we haven't yet received payment. It was due on Monday 16 November 2026 "
        "and is now overdue."
    ),
}


def wrapper(level: int, opening: str) -> tuple[str, str]:
    if level == 1:
        return "Reminder: invoice INV-1 from Acme Co", (
            f"Hello Acme Ltd,\n\n{opening}\n\n"
            f"You can view and pay it here:\n{URL}\n\n"
            "If you've already paid, thank you, and please ignore this email. "
            "If anything is unclear, just reply.\n\n"
            "Thanks,\nAcme Billing\nAcme Co\n"
        )
    if level == 2:
        return "Second reminder: invoice INV-1", (
            f"Hello Acme Ltd,\n\n{opening}\n\n"
            f"Please arrange payment using the link below:\n{URL}\n\n"
            "If there's a problem with the invoice, please reply so we can sort it out.\n\n"
            "Regards,\nAcme Billing\nAcme Co\n"
        )
    return "Invoice INV-1: payment required", (
        f"Dear Acme Ltd,\n\n{opening}\n\n"
        f"Please arrange payment promptly using the link below:\n{URL}\n\n"
        "If we don't hear from you, this will be passed to our accounts team to follow up. "
        "If you believe this invoice is incorrect, please reply and tell us why.\n\n"
        "Yours sincerely,\nAcme Billing\nAcme Co\n"
    )


@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize(("kind", "when"), sorted(OPENING, key=str))
def test_reminder_exact_text(kind: ReminderKind, when: str, level: int) -> None:
    today = BEFORE_DUE if when == "before" else AFTER_DUE
    assert render_reminder_email(kind, level, facts(today)) == wrapper(level, OPENING[kind, when])


def test_reminder_due_phrase_boundary_is_the_due_date_itself() -> None:
    on_due = render_reminder_email(ReminderKind.REMINDER, 1, facts(date(2026, 11, 16)))[1]
    day_after = render_reminder_email(ReminderKind.REMINDER, 1, facts(date(2026, 11, 17)))[1]
    assert "is due by Monday 16 November 2026," in on_due
    assert "was due on Monday 16 November 2026 and is now overdue," in day_after


@pytest.mark.parametrize(("level", "expected"), [(-1, 1), (0, 1), (1, 1), (2, 2), (3, 3), (9, 3)])
def test_reminder_level_is_clamped(level: int, expected: int) -> None:
    got = render_reminder_email(ReminderKind.REMINDER, level, facts())
    assert got == render_reminder_email(ReminderKind.REMINDER, expected, facts())


def test_reminder_dates_are_absolute() -> None:
    for kind in ReminderKind:
        for level in (1, 2, 3):
            _, body = render_reminder_email(kind, level, facts())
            assert "days" not in body.lower()
            assert "tomorrow" not in body.lower()


def test_broken_promise_needs_a_promised_date() -> None:
    with pytest.raises(ValueError, match="promised_date"):
        render_reminder_email(ReminderKind.BROKEN_PROMISE, 1, facts(promised_date=None))


def test_reminder_refuses_without_payment_link() -> None:
    with pytest.raises(ValueError, match="hosted_invoice_url"):
        render_reminder_email(ReminderKind.REMINDER, 1, facts(hosted_invoice_url=""))


FORBIDDEN = ["final notice", "legal", "court", "solicitor", "debt collect", "interest"]


@pytest.mark.parametrize("today", [BEFORE_DUE, AFTER_DUE])
@pytest.mark.parametrize("level", [1, 2, 3])
@pytest.mark.parametrize("kind", list(ReminderKind))
def test_reminders_never_use_threatening_wording(
    kind: ReminderKind, level: int, today: date
) -> None:
    subject, body = render_reminder_email(kind, level, facts(today))
    text = f"{subject}\n{body}".lower()
    assert not [w for w in FORBIDDEN if w in text]
