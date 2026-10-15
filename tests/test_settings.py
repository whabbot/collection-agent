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
