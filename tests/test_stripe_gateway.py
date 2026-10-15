"""LiveStripeGateway against a stubbed HTTP client: asserts the wire format, no network."""

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlparse

import stripe
from stripe import HTTPClient

from collection_agent.stripe_gateway import STRIPE_API_VERSION, LiveStripeGateway


class StubHTTPClient(HTTPClient):
    """Records requests and answers with canned Stripe-shaped JSON. Opens no sockets."""

    name = "stub"

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[dict[str, Any]] = []

    def request(  # type: ignore[override]
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None,
        post_data: Any = None,
        *,
        _usage: list[str] | None = None,
    ) -> tuple[str, int, Mapping[str, str]]:
        path = urlparse(url).path
        form = {k: v[0] for k, v in parse_qs(post_data or "", keep_blank_values=True).items()}
        self.requests.append(
            {"method": method, "path": path, "form": form, "headers": dict(headers or {})}
        )
        if path == "/v1/customers":
            body: dict[str, Any] = {"id": "cus_stub_1", "object": "customer"}
        elif path == "/v1/invoices":
            body = {"id": "in_stub_1", "object": "invoice", "status": "draft"}
        elif path == "/v1/invoiceitems":
            body = {"id": "ii_stub_1", "object": "invoiceitem"}
        elif path == "/v1/invoices/in_stub_1/finalize":
            body = {
                "id": "in_stub_1",
                "object": "invoice",
                "status": "open",
                "hosted_invoice_url": "https://invoice.stripe.test/i/in_stub_1",
            }
        else:
            raise AssertionError(f"unexpected Stripe request: {method} {path}")
        return json.dumps(body), 200, {"request-id": "req_stub"}

    def request_stream(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("streaming is never used")

    def close(self) -> None:
        pass


def header(req: dict[str, Any], name: str) -> str | None:
    return next((v for k, v in req["headers"].items() if k.lower() == name.lower()), None)


def test_create_flow_wire_format() -> None:
    http = StubHTTPClient()
    gw = LiveStripeGateway("sk_test_fake_not_real", http_client=http)

    customer_id = gw.create_customer("Acme Ltd", "ap@acme.example", "REF-1:customer")
    invoice_id = gw.create_invoice(
        customer_id,
        "gbp",
        1_795_000_000,
        {"reference": "REF-1", "contract_id": "CTR-1"},
        "REF-1:invoice",
    )
    gw.add_invoice_item(invoice_id, customer_id, 250000, "gbp", "Retainer", "REF-1:line:0")
    finalized = gw.finalize_invoice(invoice_id, "REF-1:finalize")

    assert (customer_id, invoice_id) == ("cus_stub_1", "in_stub_1")
    assert finalized.status == "open"
    assert finalized.hosted_invoice_url == "https://invoice.stripe.test/i/in_stub_1"

    assert [(r["method"], r["path"]) for r in http.requests] == [
        ("post", "/v1/customers"),
        ("post", "/v1/invoices"),
        ("post", "/v1/invoiceitems"),
        ("post", "/v1/invoices/in_stub_1/finalize"),
    ]
    keys = [header(r, "Idempotency-Key") for r in http.requests]
    assert keys == ["REF-1:customer", "REF-1:invoice", "REF-1:line:0", "REF-1:finalize"]
    assert all(header(r, "Stripe-Version") == STRIPE_API_VERSION for r in http.requests)

    _, inv, item, fin = (r["form"] for r in http.requests)
    assert inv["collection_method"] == "send_invoice"
    assert inv["auto_advance"] == "false"
    assert inv["due_date"] == "1795000000"
    assert inv["customer"] == "cus_stub_1"
    assert inv["metadata[reference]"] == "REF-1"
    assert inv["metadata[contract_id]"] == "CTR-1"
    assert item["invoice"] == "in_stub_1"
    assert (item["amount"], item["currency"], item["description"]) == ("250000", "gbp", "Retainer")
    assert fin["auto_advance"] == "false"

    # Customer-safety: no payment_method_types anywhere, and never the send endpoint.
    assert not any("payment_method_types" in key for r in http.requests for key in r["form"])
    assert not any(r["path"].endswith("/send") for r in http.requests)


def test_stripe_errors_propagate() -> None:
    class Failing(StubHTTPClient):
        def request(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
            body = {"error": {"type": "invalid_request_error", "message": "nope"}}
            return json.dumps(body), 400, {"request-id": "req_stub_fail"}

    gw = LiveStripeGateway("sk_test_fake_not_real", http_client=Failing())
    try:
        gw.create_customer("A", "a@b.example", "k")
    except stripe.StripeError as exc:
        assert exc.request_id == "req_stub_fail"
    else:
        raise AssertionError("expected a StripeError")
