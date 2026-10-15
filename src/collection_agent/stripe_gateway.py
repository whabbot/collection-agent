"""Thin boundary around Stripe. No Stripe objects leak out of this module."""

from dataclasses import dataclass
from typing import Protocol

import stripe

STRIPE_API_VERSION: str = stripe.api_version  # the version the installed SDK is built for


@dataclass(frozen=True)
class FinalizedInvoice:
    id: str
    status: str
    hosted_invoice_url: str | None


@dataclass(frozen=True)
class VerifiedEvent:
    """A webhook event whose signature has been checked."""

    id: str
    type: str
    livemode: bool
    object_id: str | None


class StripeGateway(Protocol):
    def create_customer(self, name: str, email: str, idempotency_key: str) -> str: ...

    def create_invoice(
        self,
        customer_id: str,
        currency: str,
        due_date: int,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> str: ...

    def add_invoice_item(
        self,
        invoice_id: str,
        customer_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> None: ...

    def finalize_invoice(self, invoice_id: str, idempotency_key: str) -> FinalizedInvoice: ...

    def get_invoice_status(self, stripe_invoice_id: str) -> str: ...


class LiveStripeGateway:
    """Real Stripe calls. Invoices are created collection_method=send_invoice with
    auto_advance=False and are only ever finalized: the send-invoice endpoint is never
    used, and payment_method_types is never passed, so Stripe emails nobody."""

    def __init__(self, api_key: str, *, http_client: stripe.HTTPClient | None = None) -> None:
        self._client = stripe.StripeClient(
            api_key, stripe_version=STRIPE_API_VERSION, http_client=http_client
        )

    def create_customer(self, name: str, email: str, idempotency_key: str) -> str:
        customer = self._client.v1.customers.create(
            {"name": name, "email": email}, {"idempotency_key": idempotency_key}
        )
        return customer.id

    def create_invoice(
        self,
        customer_id: str,
        currency: str,
        due_date: int,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> str:
        invoice = self._client.v1.invoices.create(
            {
                "customer": customer_id,
                "currency": currency,
                "collection_method": "send_invoice",
                "due_date": due_date,
                "auto_advance": False,
                "metadata": metadata,
            },
            {"idempotency_key": idempotency_key},
        )
        return invoice.id

    def add_invoice_item(
        self,
        invoice_id: str,
        customer_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> None:
        self._client.v1.invoice_items.create(
            {
                "customer": customer_id,
                "invoice": invoice_id,
                "amount": amount_minor,
                "currency": currency,
                "description": description,
            },
            {"idempotency_key": idempotency_key},
        )

    def finalize_invoice(self, invoice_id: str, idempotency_key: str) -> FinalizedInvoice:
        invoice = self._client.v1.invoices.finalize_invoice(
            invoice_id, {"auto_advance": False}, {"idempotency_key": idempotency_key}
        )
        return FinalizedInvoice(
            id=invoice.id,
            status=str(invoice.status),
            hosted_invoice_url=invoice.hosted_invoice_url,
        )

    def get_invoice_status(self, stripe_invoice_id: str) -> str:
        """Read-only: the invoice's current Stripe status (draft/open/paid/void/uncollectible)."""
        return str(self._client.v1.invoices.retrieve(stripe_invoice_id).status)


class WebhookSignatureError(ValueError):
    """The payload failed signature verification or is not a Stripe event."""


def verify_webhook(
    payload: bytes, signature_header: str | None, secret: str, *, tolerance: int = 300
) -> VerifiedEvent:
    """Verify `Stripe-Signature` against the raw body and parse the event.

    The StripeClient is only used for its `construct_event` helper; it never makes a request,
    so a placeholder API key is enough.
    """
    client = stripe.StripeClient("unused-for-webhook-verification")
    try:
        event = client.construct_event(payload, signature_header, secret, tolerance)
        data = event.data.object.to_dict()
        object_id = data.get("id")
        return VerifiedEvent(
            id=event.id,
            type=event.type,
            livemode=bool(event.livemode),
            object_id=object_id if isinstance(object_id, str) else None,
        )
    except (stripe.SignatureVerificationError, ValueError, KeyError, AttributeError) as exc:
        raise WebhookSignatureError(str(exc)) from exc
