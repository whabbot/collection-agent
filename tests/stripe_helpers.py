"""Webhook signing and fixture loading for tests. The secret is obviously fake."""

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

WEBHOOK_SECRET = "whsec_test_fake"
FIXTURES = Path(__file__).parent / "fixtures" / "stripe"


def load_fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


def sign(payload: bytes, secret: str = WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """A Stripe-Signature header value: t=<ts>,v1=<HMAC-SHA256(secret, f"{t}.{payload}")>."""
    t = int(time.time()) if timestamp is None else timestamp
    digest = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={digest}"
