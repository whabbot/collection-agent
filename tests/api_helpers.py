"""Build a TestClient with every external dependency overridden."""

from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.app import create_app
from collection_agent.classifier import ReplyClassifier
from collection_agent.cognee_client import CogneeGateway
from collection_agent.deps import (
    get_cognee,
    get_email_sender,
    get_now,
    get_optional_stripe_gateway,
    get_policy_extractor,
    get_reply_classifier,
    get_session,
    get_session_factory,
    get_stripe_gateway,
    get_today,
)
from collection_agent.email_sender import EmailSender
from collection_agent.policy_extraction import PolicyExtractor
from collection_agent.settings import Settings, get_settings
from collection_agent.stripe_gateway import StripeGateway

ADMIN_TOKEN = "admin_test_fake"
AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}
INBOUND_USER = "inbound_test_user"
INBOUND_PASSWORD = "inbound_test_pass_fake"
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class PoisonSession:
    """Stands in for the DB in tests that must not touch it: any use fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"database touched: session.{name}")


def make_settings(**overrides: str | bool | None) -> Settings:
    base: dict[str, str | bool | None] = {
        "database_url": "postgresql+psycopg://x:x@127.0.0.1:1/x",
        "admin_api_token": ADMIN_TOKEN,
        "stripe_secret_key": "sk_test_fake_not_real",
        "stripe_webhook_secret": "whsec_test_fake",
        "postmark_server_token": "pm_test_fake_not_real",
        "postmark_message_stream": "outbound",
        "email_from_address": "billing@billing.example.test",
        "email_from_name": "Acme Billing",
        "reply_to_domain": "billing.example.test",
        "company_name": "Acme Co",
        "typesafe_api_key": "ts_test_fake_not_real",
        "inbound_webhook_user": INBOUND_USER,
        "inbound_webhook_password": INBOUND_PASSWORD,
    }
    return Settings.model_construct(**{**base, **overrides})


def make_client(
    *,
    session: Session | None = None,
    gateway: StripeGateway | None = None,
    email_sender: EmailSender | None = None,
    settings: Settings | None = None,
    today: date = date(2026, 10, 15),
    classifier: ReplyClassifier | None = None,
    now: datetime = NOW,
    cognee: CogneeGateway | None = None,
    extractor: PolicyExtractor | None = None,
    session_factory: sessionmaker[Session] | None = None,
    real_clock: bool = False,
) -> TestClient:
    app = create_app()

    def _session() -> Iterator[Any]:
        yield session if session is not None else PoisonSession()

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_settings] = lambda: settings or make_settings()
    if not real_clock:  # `real_clock` lets demo-mode tests exercise the real clock dependencies
        app.dependency_overrides[get_today] = lambda: today
        app.dependency_overrides[get_now] = lambda: now
    if session_factory is not None:
        app.dependency_overrides[get_session_factory] = lambda: session_factory
    # Always overridden, so a test can never build a real Jev or Stripe client by accident.
    app.dependency_overrides[get_reply_classifier] = lambda: classifier
    app.dependency_overrides[get_cognee] = lambda: cognee
    app.dependency_overrides[get_policy_extractor] = lambda: extractor
    app.dependency_overrides[get_optional_stripe_gateway] = lambda: gateway
    if gateway is not None:
        app.dependency_overrides[get_stripe_gateway] = lambda: gateway
    if email_sender is not None:
        app.dependency_overrides[get_email_sender] = lambda: email_sender
    return TestClient(app)
