"""Application settings, read from the environment (and `.env` if present)."""

from functools import lru_cache

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
