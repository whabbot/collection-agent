"""`collection-agent demo reset`: wipe the demo's working data. Guarded three ways: DEMO_MODE
must be on, the database name must contain `collection_agent`, and the person must type
`reset` (unless `--yes`)."""

from collections.abc import Callable

from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.db.models import (
    ChasePolicyRow,
    Contract,
    Customer,
    DailyRun,
    DemoSentEmail,
    InboundMessage,
    Invoice,
    OutboundEmail,
    PolicyDocument,
    PolicyExtraction,
    Reminder,
    StripeEvent,
)
from collection_agent.settings import Settings

REQUIRED_DB_NAME_PART = "collection_agent"
CONFIRM_WORD = "reset"

# Children before parents. The simulated clock is left alone.
_DELETE_ORDER: tuple[type, ...] = (
    PolicyExtraction,
    PolicyDocument,
    ChasePolicyRow,
    OutboundEmail,
    InboundMessage,
    Reminder,
    Contract,
    Invoice,
    Customer,
    StripeEvent,
    DailyRun,
    DemoSentEmail,
)


class DemoResetRefused(Exception):
    """A guard stopped the reset. The message never contains the database URL."""


def check_guards(settings: Settings) -> None:
    if not settings.demo_mode:
        raise DemoResetRefused("refusing to reset: DEMO_MODE is not true")
    name = make_url(settings.database_url).database or ""
    if REQUIRED_DB_NAME_PART not in name:
        raise DemoResetRefused(
            f"refusing to reset: the database name must contain '{REQUIRED_DB_NAME_PART}'"
        )


def confirm_and_reset(
    settings: Settings,
    factory: sessionmaker[Session],
    *,
    assume_yes: bool,
    confirm: Callable[[str], str],
) -> dict[str, int] | None:
    """Row counts deleted per table, or None if the person did not confirm."""
    check_guards(settings)
    if not assume_yes:
        answer = confirm(f"This deletes all demo data. Type '{CONFIRM_WORD}' to continue: ")
        if answer.strip() != CONFIRM_WORD:
            return None
    deleted: dict[str, int] = {}
    with factory() as session:
        for model in _DELETE_ORDER:
            table = model.__table__  # type: ignore[attr-defined]
            before = session.scalar(select(func.count()).select_from(table)) or 0
            session.execute(delete(table))
            deleted[table.name] = before
        session.commit()
    return deleted
