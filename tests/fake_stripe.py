"""In-memory StripeGateway mimicking Stripe idempotency keys. Never touches the network."""

import json
from collections.abc import Callable
from typing import Any

import stripe

from collection_agent.stripe_gateway import FinalizedInvoice


class FakeStripeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []  # every call, incl. replays
        self.created: list[str] = []  # method names that actually created something
        self.fail_on: dict[str, Exception] = {}
        self.before: dict[str, Callable[[], None]] = {}
        self.statuses: dict[str, str] = {}  # stripe invoice id -> status; default "open"
        self.status_errors: dict[str, Exception] = {}
        self._by_key: dict[str, tuple[str, Any]] = {}
        self._n = 0

    # -- helpers --------------------------------------------------------------------------
    def _call(self, method: str, key: str, params: dict[str, Any], make: Callable[[], Any]) -> Any:
        self.calls.append((method, {**params, "idempotency_key": key}))
        if method in self.fail_on:
            raise self.fail_on[method]
        fingerprint = json.dumps(params, sort_keys=True)
        if key in self._by_key:
            stored_fp, stored = self._by_key[key]
            if stored_fp != fingerprint:
                raise stripe.IdempotencyError(
                    f"Keys for idempotent requests can only be used with the same parameters: {key}"
                )
            return stored
        if method in self.before:
            self.before[method]()
        result = make()
        self.created.append(method)
        self._by_key[key] = (fingerprint, result)
        return result

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}_fake{self._n}"

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def count_created(self, method: str) -> int:
        return self.created.count(method)

    # -- StripeGateway --------------------------------------------------------------------
    def create_customer(self, name: str, email: str, idempotency_key: str) -> str:
        params = {"name": name, "email": email}
        return str(
            self._call("create_customer", idempotency_key, params, lambda: self._next("cus"))
        )

    def create_invoice(
        self,
        customer_id: str,
        currency: str,
        due_date: int,
        metadata: dict[str, str],
        idempotency_key: str,
    ) -> str:
        params = {
            "customer_id": customer_id,
            "currency": currency,
            "due_date": due_date,
            "metadata": metadata,
        }
        return str(self._call("create_invoice", idempotency_key, params, lambda: self._next("in")))

    def add_invoice_item(
        self,
        invoice_id: str,
        customer_id: str,
        amount_minor: int,
        currency: str,
        description: str,
        idempotency_key: str,
    ) -> None:
        params = {
            "invoice_id": invoice_id,
            "customer_id": customer_id,
            "amount_minor": amount_minor,
            "currency": currency,
            "description": description,
        }
        self._call("add_invoice_item", idempotency_key, params, lambda: self._next("ii"))

    def finalize_invoice(self, invoice_id: str, idempotency_key: str) -> FinalizedInvoice:
        result = self._call(
            "finalize_invoice",
            idempotency_key,
            {"invoice_id": invoice_id},
            lambda: FinalizedInvoice(
                id=invoice_id,
                status="open",
                hosted_invoice_url=f"https://invoice.stripe.test/i/{invoice_id}",
            ),
        )
        assert isinstance(result, FinalizedInvoice)
        return result

    def get_invoice_status(self, stripe_invoice_id: str) -> str:
        self.calls.append(("get_invoice_status", {"stripe_invoice_id": stripe_invoice_id}))
        if stripe_invoice_id in self.status_errors:
            raise self.status_errors[stripe_invoice_id]
        return self.statuses.get(stripe_invoice_id, "open")
