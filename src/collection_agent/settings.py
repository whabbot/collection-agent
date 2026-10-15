"""Application settings, read from the environment (and `.env` if present)."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    admin_api_token: str | None = None
    postmark_server_token: str | None = None
    postmark_message_stream: str = "outbound"
    email_from_address: str | None = None
    email_from_name: str | None = None
    reply_to_domain: str | None = None
    company_name: str | None = None
    typesafe_api_key: str | None = None
    jev_model: str = "jev-1.13.0"
    classifier_min_confidence: float = Field(default=0.6, ge=0, le=1)
    inbound_webhook_user: str | None = None
    inbound_webhook_password: str | None = None


class TypesafeSettings(BaseSettings):
    """Just what the classifier needs, so `eval-classifier` doesn't require DATABASE_URL."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    typesafe_api_key: str | None = None
    jev_model: str = "jev-1.13.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
