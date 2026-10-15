from datetime import date

import pytest

from collection_agent.db.models import OutboundKind
from collection_agent.templates import REPLY_KINDS, ReplyFacts, render_reply_body

URL = "https://invoice.stripe.test/i/in_fake1"
SIGNOFF = "\n\nThanks,\nAcme Billing\nAcme Co\n"
FORBIDDEN = ["final notice", "legal", "court", "solicitor", "debt collect", "interest"]


def facts(**kw: object) -> ReplyFacts:
    base: dict[str, object] = {
        "customer_name": "Acme Ltd",
        "reference": "INV-1001",
        "amount_minor": 120_000,
        "currency": "GBP",
        "from_name": "Acme Billing",
        "company_name": "Acme Co",
        "hosted_invoice_url": URL,
        "promised_date": date(2026, 11, 6),
    }
    return ReplyFacts(**{**base, **kw})  # type: ignore[arg-type]


def test_paid_thanks_exact_text() -> None:
    assert render_reply_body(OutboundKind.REPLY_PAID_THANKS, facts()) == (
        "Hello Acme Ltd,\n\n"
        "Thank you. We can confirm that invoice INV-1001 for £1,200.00 has been paid. "
        "No further action is needed." + SIGNOFF
    )


def test_remittance_request_exact_text() -> None:
    assert render_reply_body(OutboundKind.REPLY_REMITTANCE_REQUEST, facts()) == (
        "Hello Acme Ltd,\n\n"
        "Thank you for letting us know. We haven't been able to match a payment to invoice "
        "INV-1001 for £1,200.00 yet. Could you reply with the payment date and the remittance "
        "or transaction reference, so we can find it?" + SIGNOFF
    )


def test_promise_ack_exact_text_with_absolute_date_and_link() -> None:
    assert render_reply_body(OutboundKind.REPLY_PROMISE_ACK, facts()) == (
        "Hello Acme Ltd,\n\n"
        "Thank you for letting us know. We've noted that you expect to pay invoice INV-1001 for "
        f"£1,200.00 by Friday 6 November 2026. You can pay here:\n{URL}" + SIGNOFF
    )


def test_holding_exact_text() -> None:
    assert render_reply_body(OutboundKind.REPLY_HOLDING, facts()) == (
        "Hello Acme Ltd,\n\n"
        "Thank you for your message about invoice INV-1001. A member of our team will look at "
        "it and get back to you." + SIGNOFF
    )


def test_resend_exact_text_without_and_with_contract() -> None:
    plain = (
        "Hello Acme Ltd,\n\n"
        f"As requested, you can view and pay invoice INV-1001 for £1,200.00 here:\n{URL}"
    )
    assert render_reply_body(OutboundKind.REPLY_RESEND, facts()) == plain + SIGNOFF
    assert render_reply_body(OutboundKind.REPLY_RESEND, facts(attach_contract=True)) == (
        plain + "\n\nYour contract is attached." + SIGNOFF
    )


def test_promise_ack_and_resend_refuse_without_a_link() -> None:
    with pytest.raises(ValueError, match="payment link"):
        render_reply_body(OutboundKind.REPLY_PROMISE_ACK, facts(hosted_invoice_url=None))
    with pytest.raises(ValueError, match="payment link"):
        render_reply_body(OutboundKind.REPLY_RESEND, facts(hosted_invoice_url=None))


def test_promise_ack_refuses_without_a_date() -> None:
    with pytest.raises(ValueError, match="date"):
        render_reply_body(OutboundKind.REPLY_PROMISE_ACK, facts(promised_date=None))


def test_non_reply_kind_refused() -> None:
    with pytest.raises(ValueError, match="not a reply kind"):
        render_reply_body(OutboundKind.REMINDER, facts())


@pytest.mark.parametrize("kind", REPLY_KINDS)
def test_no_banned_words_and_no_relative_dates(kind: OutboundKind) -> None:
    body = render_reply_body(kind, facts(attach_contract=True)).lower()
    assert not [w for w in FORBIDDEN if w in body]
    assert "tomorrow" not in body
    assert "days" not in body


def test_customer_supplied_text_is_not_an_input() -> None:
    # The only free-text inputs are our own records; there is no parameter for the email body.
    assert set(ReplyFacts.__dataclass_fields__) == {
        "customer_name",
        "reference",
        "amount_minor",
        "currency",
        "from_name",
        "company_name",
        "hosted_invoice_url",
        "promised_date",
        "attach_contract",
    }
