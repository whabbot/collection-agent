"""FastAPI dependencies shared by the routers. Tests override these, never the internals."""

import secrets
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.classifier import JevClassifier, ReplyClassifier
from collection_agent.classifier_claude import ClaudeClassifier
from collection_agent.cognee_client import CogneeGateway, LiveCognee
from collection_agent.db.engine import make_engine, make_session_factory
from collection_agent.demo_support import LoggingEmailSender, clock_now, clock_today
from collection_agent.email_sender import EmailSender, PostmarkSender
from collection_agent.policy_extraction import ClaudeExtractor, PolicyExtractor
from collection_agent.settings import Settings, get_settings
from collection_agent.stripe_gateway import LiveStripeGateway, StripeGateway

LONDON = ZoneInfo("Europe/London")


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine(get_settings().database_url))


def get_session() -> Iterator[Session]:
    with _session_factory()() as session:
        yield session


def get_session_factory() -> sessionmaker[Session]:
    """For code that opens its own sessions (the daily run). Tests override this."""
    return _session_factory()


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
SessionFactoryDep = Annotated[sessionmaker[Session], Depends(get_session_factory)]


def require_admin(
    settings: SettingsDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    """Bearer-token auth for internal endpoints. Fails closed when no token is configured."""
    expected = settings.admin_api_token
    if not expected:
        raise HTTPException(status_code=503, detail={"error": "admin_api_token_not_configured"})
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail={"error": "unauthorized"})


AdminDep = Annotated[None, Depends(require_admin)]


@lru_cache
def _live_gateway(api_key: str) -> LiveStripeGateway:
    return LiveStripeGateway(api_key)


def get_stripe_gateway(settings: SettingsDep) -> StripeGateway:
    if not settings.stripe_secret_key:
        raise HTTPException(status_code=503, detail={"error": "stripe_not_configured"})
    return _live_gateway(settings.stripe_secret_key)


GatewayDep = Annotated[StripeGateway, Depends(get_stripe_gateway)]


def get_today(settings: SettingsDep, session: SessionDep) -> date:
    """Today's date in Europe/London; in demo mode the simulated clock's date. Override in tests
    to control the clock."""
    if settings.demo_mode:
        return clock_today(session)
    return datetime.now(LONDON).date()


TodayDep = Annotated[date, Depends(get_today)]


@lru_cache
def _postmark_sender(token: str, stream: str) -> PostmarkSender:
    return PostmarkSender(token, stream)


def get_email_sender(settings: SettingsDep, session: SessionDep) -> EmailSender:
    if settings.email_sender == "log":
        if not settings.demo_mode:  # also refused when the settings are loaded; belt and braces
            raise HTTPException(
                status_code=503, detail={"error": "log_email_sender_requires_demo_mode"}
            )
        return LoggingEmailSender(session)
    if not settings.postmark_server_token:
        raise HTTPException(status_code=503, detail={"error": "postmark_not_configured"})
    return _postmark_sender(settings.postmark_server_token, settings.postmark_message_stream)


EmailSenderDep = Annotated[EmailSender, Depends(get_email_sender)]


@dataclass(frozen=True)
class EmailIdentity:
    from_address: str
    from_name: str
    reply_to_domain: str
    company_name: str


_IDENTITY_SETTINGS = (
    ("EMAIL_FROM_ADDRESS", "email_from_address"),
    ("EMAIL_FROM_NAME", "email_from_name"),
    ("REPLY_TO_DOMAIN", "reply_to_domain"),
    ("COMPANY_NAME", "company_name"),
)


def missing_identity_settings(settings: Settings) -> list[str]:
    """Names of the environment variables the email identity needs and does not have."""
    return [env for env, attr in _IDENTITY_SETTINGS if not getattr(settings, attr)]


def identity_from_settings(settings: Settings) -> EmailIdentity | None:
    if (
        settings.email_from_address
        and settings.email_from_name
        and settings.reply_to_domain
        and settings.company_name
    ):
        return EmailIdentity(
            settings.email_from_address,
            settings.email_from_name,
            settings.reply_to_domain,
            settings.company_name,
        )
    return None


def get_email_identity(settings: SettingsDep) -> EmailIdentity:
    identity = identity_from_settings(settings)
    if identity is None:
        raise HTTPException(status_code=503, detail={"error": "email_not_configured"})
    return identity


EmailIdentityDep = Annotated[EmailIdentity, Depends(get_email_identity)]


def get_now(settings: SettingsDep, session: SessionDep) -> datetime:
    """The current moment, timezone-aware; in demo mode the simulated date at the real time of
    day. Override in tests to control the clock."""
    if settings.demo_mode:
        return clock_now(session)
    return datetime.now(UTC)


NowDep = Annotated[datetime, Depends(get_now)]


# --- optional dependencies for the inbound webhook ---------------------------------------------
# A reply must be stored even when Stripe, the email identity or the classifier is not
# configured (Postmark would otherwise retry, then drop it), so these return None instead of
# raising 503. The webhook sends the message to a human when it needs one that is missing.


def get_optional_stripe_gateway(settings: SettingsDep) -> StripeGateway | None:
    if not settings.stripe_secret_key:
        return None
    return _live_gateway(settings.stripe_secret_key)


def get_optional_email_identity(settings: SettingsDep) -> EmailIdentity | None:
    return identity_from_settings(settings)


@lru_cache
def _jev_classifier(api_key: str, model: str) -> JevClassifier:
    return JevClassifier(api_key, model)


@lru_cache
def _claude_classifier(api_key: str, model: str) -> ClaudeClassifier:
    return ClaudeClassifier(api_key, model)


def get_reply_classifier(settings: SettingsDep) -> ReplyClassifier | None:
    if settings.classifier == "claude":
        if not settings.anthropic_api_key:
            return None
        return _claude_classifier(settings.anthropic_api_key, settings.anthropic_model)
    if not settings.typesafe_api_key:
        return None
    return _jev_classifier(settings.typesafe_api_key, settings.jev_model)


OptionalGatewayDep = Annotated[StripeGateway | None, Depends(get_optional_stripe_gateway)]
OptionalIdentityDep = Annotated[EmailIdentity | None, Depends(get_optional_email_identity)]
ClassifierDep = Annotated[ReplyClassifier | None, Depends(get_reply_classifier)]


# --- policy ingest: both are optional, each endpoint decides what a missing one means ------------


@lru_cache
def _live_cognee(base_url: str, api_key: str) -> LiveCognee:
    return LiveCognee(base_url, api_key)


def get_cognee(settings: SettingsDep) -> CogneeGateway | None:
    if not settings.cognee_base_url or not settings.cognee_api_key:
        return None
    return _live_cognee(settings.cognee_base_url, settings.cognee_api_key)


@lru_cache
def _claude_extractor(api_key: str, model: str) -> ClaudeExtractor:
    return ClaudeExtractor(api_key, model)


def get_policy_extractor(settings: SettingsDep) -> PolicyExtractor | None:
    if not settings.anthropic_api_key:
        return None
    return _claude_extractor(settings.anthropic_api_key, settings.anthropic_model)


CogneeDep = Annotated[CogneeGateway | None, Depends(get_cognee)]
ExtractorDep = Annotated[PolicyExtractor | None, Depends(get_policy_extractor)]
