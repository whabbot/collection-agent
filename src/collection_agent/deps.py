"""FastAPI dependencies shared by the routers. Tests override these, never the internals."""

import secrets
from collections.abc import Iterator
from datetime import date, datetime
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.db.engine import make_engine, make_session_factory
from collection_agent.settings import Settings, get_settings
from collection_agent.stripe_gateway import LiveStripeGateway, StripeGateway

LONDON = ZoneInfo("Europe/London")


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine(get_settings().database_url))


def get_session() -> Iterator[Session]:
    with _session_factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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


def get_today() -> date:
    """Today's date in Europe/London. Override in tests to control the clock."""
    return datetime.now(LONDON).date()


TodayDep = Annotated[date, Depends(get_today)]
