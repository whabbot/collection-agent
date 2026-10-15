"""Builders for Postmark inbound payloads and the DB rows they refer to."""

import base64
import itertools
from typing import Any

from sqlalchemy.orm import Session

from collection_agent.db.models import Customer, Invoice, OutboundEmail, OutboundStatus
from tests.api_helpers import INBOUND_PASSWORD, INBOUND_USER

_counter = itertools.count(1)

BASIC = {
    "Authorization": "Basic "
    + base64.b64encode(f"{INBOUND_USER}:{INBOUND_PASSWORD}".encode()).decode()
}


def headers(**named: str) -> list[dict[str, str]]:
    """Postmark's Headers array from `Name=value` pairs (underscores become hyphens)."""
    return [{"Name": k.replace("_", "-"), "Value": v} for k, v in named.items()]


def payload(
    *,
    text: str = "Hello, we will pay on Friday.",
    stripped: str | None = None,
    subject: str = "Invoice INV-1",
    message_id: str | None = "auto",
    postmark_id: str | None = None,
    from_email: str = "ap@acme.test",
    mailbox_hash: str = "",
    extra_headers: list[dict[str, str]] | None = None,
    attachments: int = 0,
) -> dict[str, Any]:
    n = next(_counter)
    hdrs: list[dict[str, str]] = []
    if message_id == "auto":
        hdrs.append({"Name": "Message-ID", "Value": f"<reply-{n}@mail.acme.test>"})
    elif message_id:
        hdrs.append({"Name": "Message-ID", "Value": message_id})
    hdrs.extend(extra_headers or [])
    return {
        "From": f"Acme AP <{from_email}>",
        "FromName": "Acme AP",
        "FromFull": {"Email": from_email, "Name": "Acme AP", "MailboxHash": ""},
        "To": f"reply+{mailbox_hash}@inbound.example.test",
        "ToFull": [
            {
                "Email": f"reply+{mailbox_hash}@inbound.example.test",
                "Name": "",
                "MailboxHash": mailbox_hash,
            }
        ],
        "OriginalRecipient": f"reply+{mailbox_hash}@inbound.example.test",
        "MailboxHash": mailbox_hash,
        "Subject": subject,
        "MessageID": postmark_id or f"00000000-0000-4000-8000-{n:012d}",
        "Date": "Fri, 16 Oct 2026 09:00:00 +0100",
        "TextBody": text,
        "StrippedTextReply": text if stripped is None else stripped,
        "Headers": hdrs,
        "Attachments": [{"Name": f"a{i}.pdf", "Content": "AAAA"} for i in range(attachments)],
    }


def sent_outbox(session: Session, inv: Invoice, provider_message_id: str) -> OutboundEmail:
    cust = session.get(Customer, inv.customer_id)
    assert cust is not None
    row = OutboundEmail(
        invoice_id=inv.id,
        kind="reminder",
        to_address=cust.email,
        from_address="billing@billing.example.test",
        from_name="Acme Billing",
        reply_to=f"reply+{inv.reply_token}@billing.example.test",
        subject="Reminder",
        body_text="x",
        attach_contract=False,
        status=OutboundStatus.SENT,
        provider_message_id=provider_message_id,
    )
    session.add(row)
    session.flush()
    return row
