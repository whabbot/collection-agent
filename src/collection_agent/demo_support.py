"""Demo-mode building blocks that the shared dependencies need: the simulated clock and the
`log` email sender. Imports nothing from `deps`, so `deps` can import this.

Nothing here is used unless `DEMO_MODE=true`.
"""

import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from collection_agent.db.models import DemoClock, DemoSentEmail
from collection_agent.email_sender import OutgoingEmail, SentEmail

LONDON = ZoneInfo("Europe/London")
MAX_ADVANCE_DAYS = 366


def real_today() -> date:
    return datetime.now(LONDON).date()


def stored_clock_date(session: Session) -> date | None:
    return session.scalar(select(DemoClock.today).where(DemoClock.id == 1))


def clock_today(session: Session) -> date:
    """The simulated date; the real London date until someone sets it. Reads only."""
    return stored_clock_date(session) or real_today()


def clock_now(session: Session) -> datetime:
    """The simulated date at the real current London time of day (timezone-aware)."""
    real = datetime.now(LONDON)
    return datetime.combine(clock_today(session), real.timetz().replace(tzinfo=None), LONDON)


def set_clock(session: Session, today: date) -> date:
    """Store the simulated date (one row, upserted). Does not commit."""
    row = session.get(DemoClock, 1)
    if row is None:
        session.add(DemoClock(id=1, today=today))
    else:
        row.today = today
    session.flush()
    return today


def advance_clock(session: Session, days: int) -> date:
    """Move the simulated date forward by calendar days. Does not commit."""
    return set_clock(session, clock_today(session) + timedelta(days=days))


class LoggingEmailSender:
    """Records each "sent" email in `demo_sent_emails` instead of sending it.

    It adds to the caller's session and does not commit: the approve endpoint's own commit
    makes the record and the outbox status change atomic.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def send(self, message: OutgoingEmail) -> SentEmail:
        provider_id = f"log-{uuid.uuid4()}"
        self._session.add(
            DemoSentEmail(
                to_address=message.to,
                subject=message.subject,
                body_text=message.text_body,
                attachment_names=[a.name for a in message.attachments],
                provider_message_id=provider_id,
            )
        )
        self._session.flush()
        return SentEmail(provider_message_id=provider_id)
