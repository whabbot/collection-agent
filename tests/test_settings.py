import pytest

from collection_agent.settings import Settings


def test_database_url_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    assert Settings().database_url == "postgresql+psycopg://u:p@localhost/db"


def test_stripe_and_admin_settings_are_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    for name in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "ADMIN_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.stripe_secret_key, s.stripe_webhook_secret, s.admin_api_token) == (None, None, None)


def test_stripe_and_admin_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake_not_real")
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "whsec_test_fake")
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin_test_fake")
    s = Settings()
    assert s.stripe_secret_key == "sk_test_fake_not_real"
    assert s.stripe_webhook_secret == "whsec_test_fake"
    assert s.admin_api_token == "admin_test_fake"


def test_email_settings_optional_with_stream_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    for name in (
        "POSTMARK_SERVER_TOKEN",
        "POSTMARK_MESSAGE_STREAM",
        "EMAIL_FROM_ADDRESS",
        "EMAIL_FROM_NAME",
        "REPLY_TO_DOMAIN",
        "COMPANY_NAME",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert (s.postmark_server_token, s.email_from_address, s.email_from_name) == (None,) * 3
    assert (s.reply_to_domain, s.company_name) == (None, None)
    assert s.postmark_message_stream == "outbound"


def test_email_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("POSTMARK_SERVER_TOKEN", "pm_test_fake_not_real")
    monkeypatch.setenv("POSTMARK_MESSAGE_STREAM", "broadcast")
    monkeypatch.setenv("COMPANY_NAME", "Acme Co")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.postmark_server_token == "pm_test_fake_not_real"
    assert s.postmark_message_stream == "broadcast"
    assert s.company_name == "Acme Co"


def test_inbound_and_classifier_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    for name in (
        "TYPESAFE_API_KEY",
        "JEV_MODEL",
        "CLASSIFIER_MIN_CONFIDENCE",
        "INBOUND_WEBHOOK_USER",
        "INBOUND_WEBHOOK_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.typesafe_api_key is None
    assert s.jev_model == "jev-1.13.0"
    assert s.classifier_min_confidence == 0.6
    assert (s.inbound_webhook_user, s.inbound_webhook_password) == (None, None)


def test_inbound_and_classifier_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts_test_fake_not_real")
    monkeypatch.setenv("JEV_MODEL", "jev-1.14.0")
    monkeypatch.setenv("CLASSIFIER_MIN_CONFIDENCE", "0.75")
    monkeypatch.setenv("INBOUND_WEBHOOK_USER", "u_fake")
    monkeypatch.setenv("INBOUND_WEBHOOK_PASSWORD", "p_fake")
    s = Settings()
    assert s.typesafe_api_key == "ts_test_fake_not_real"
    assert s.jev_model == "jev-1.14.0"
    assert s.classifier_min_confidence == 0.75
    assert (s.inbound_webhook_user, s.inbound_webhook_password) == ("u_fake", "p_fake")


@pytest.mark.parametrize("value", ["-0.1", "1.5"])
def test_min_confidence_must_be_a_probability(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    monkeypatch.setenv("CLASSIFIER_MIN_CONFIDENCE", value)
    with pytest.raises(ValueError, match="classifier_min_confidence"):
        Settings()
