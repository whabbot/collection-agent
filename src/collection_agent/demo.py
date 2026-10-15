"""The demo page and its helpers. Everything here exists only when `DEMO_MODE=true`.

Without it every route in this module answers 404, as if it were not installed. There are three
helpers (the simulated clock, "run daily now", "simulate a customer reply"); each goes through
the real code. There is deliberately no "mark paid": paid comes only from Stripe.
"""

import json
import logging
import uuid
from datetime import date
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from collection_agent.daily import run_daily
from collection_agent.db.models import (
    Customer,
    DemoSentEmail,
    InboundMessage,
    Invoice,
    OutboundEmail,
    OutboundStatus,
)
from collection_agent.db.repository import NoApprovedPolicyError
from collection_agent.demo_support import (
    MAX_ADVANCE_DAYS,
    advance_clock,
    clock_today,
    set_clock,
    stored_clock_date,
)
from collection_agent.deps import (
    AdminDep,
    ClassifierDep,
    EmailIdentityDep,
    GatewayDep,
    NowDep,
    OptionalGatewayDep,
    OptionalIdentityDep,
    SessionDep,
    SessionFactoryDep,
    SettingsDep,
    TodayDep,
)
from collection_agent.inbound_api import process_inbound

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SAMPLE_POLICY = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "policy_sample.txt"


def require_demo(settings: SettingsDep) -> None:
    """404 unless DEMO_MODE=true. Runs before authentication so a production deployment does
    not even reveal that these routes exist."""
    if not settings.demo_mode:
        raise HTTPException(status_code=404, detail={"error": "not_found"})


router = APIRouter(dependencies=[Depends(require_demo)])


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


# --- the page ----------------------------------------------------------------------------------


@router.get("/demo", response_model=None)
def page() -> Response:
    return FileResponse(STATIC_DIR / "demo.html", media_type="text/html")


@router.get("/demo/demo.js", response_model=None)
def script() -> Response:
    return FileResponse(STATIC_DIR / "demo.js", media_type="text/javascript")


@router.get("/demo/demo.css", response_model=None)
def stylesheet() -> Response:
    return FileResponse(STATIC_DIR / "demo.css", media_type="text/css")


# --- the simulated clock -----------------------------------------------------------------------


def _clock_body(session: Session) -> dict[str, Any]:
    stored = stored_clock_date(session)
    today = clock_today(session)
    return {
        "today": today.isoformat(),
        "weekday": today.strftime("%A"),
        "is_set": stored is not None,
    }


class ClockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    today: date


class AdvanceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: Annotated[int, Field(ge=1, le=MAX_ADVANCE_DAYS)]


@router.get("/demo/api/clock", response_model=None)
def get_clock(_admin: AdminDep, session: SessionDep) -> Response:
    return JSONResponse(_clock_body(session))


@router.post("/demo/api/clock", response_model=None)
def post_clock(body: ClockIn, _admin: AdminDep, session: SessionDep) -> Response:
    set_clock(session, body.today)
    session.commit()
    return JSONResponse(_clock_body(session))


@router.post("/demo/api/clock/advance", response_model=None)
def post_advance(body: AdvanceIn, _admin: AdminDep, session: SessionDep) -> Response:
    advance_clock(session, body.days)
    session.commit()
    return JSONResponse(_clock_body(session))


# --- run the daily job now ---------------------------------------------------------------------


@router.post("/demo/api/run-daily", response_model=None)
def post_run_daily(
    _admin: AdminDep,
    gateway: GatewayDep,  # fail closed exactly like the CLI: no Stripe key, no run
    identity: EmailIdentityDep,
    factory: SessionFactoryDep,
    session: SessionDep,
    today: TodayDep,
) -> Response:
    session.commit()  # end this request's transaction; the run uses its own sessions
    try:
        report = run_daily(factory, gateway, identity, today)
    except NoApprovedPolicyError:
        return _error("no_approved_policy", 409)
    except Exception as exc:
        logger.error("demo run-daily failed: %s", type(exc).__name__)
        return _error("daily_run_failed", 500)
    return JSONResponse(report.to_dict())


# --- simulate a customer reply ------------------------------------------------------------------


class SimulateReplyIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    invoice_id: uuid.UUID
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)]
    from_email: Annotated[str, StringConstraints(strip_whitespace=True, max_length=320)] | None = (
        None
    )


def build_reply_payload(
    invoice: Invoice, customer: Customer, last_sent: OutboundEmail | None, body: SimulateReplyIn
) -> tuple[dict[str, Any], str]:
    """A Postmark-inbound-shaped payload, and the Message-ID it carries (without brackets)."""
    message_id = f"demo-{uuid.uuid4()}@demo.local"
    sender = body.from_email or customer.email
    subject = f"Re: {last_sent.subject}" if last_sent else f"Re: Invoice {invoice.reference}"
    headers = [{"Name": "Message-ID", "Value": f"<{message_id}>"}]
    if last_sent is not None and last_sent.provider_message_id:
        headers.append({"Name": "In-Reply-To", "Value": f"<{last_sent.provider_message_id}>"})
        headers.append({"Name": "References", "Value": f"<{last_sent.provider_message_id}>"})
    to = f"reply+{invoice.reply_token}@demo.local"
    return (
        {
            "From": sender,
            "FromName": customer.name,
            "FromFull": {"Email": sender, "Name": customer.name, "MailboxHash": ""},
            "To": to,
            "ToFull": [{"Email": to, "Name": "", "MailboxHash": invoice.reply_token}],
            "OriginalRecipient": to,
            "MailboxHash": invoice.reply_token,
            "Subject": subject,
            "MessageID": str(uuid.uuid4()),
            "TextBody": body.text,
            "StrippedTextReply": body.text,
            "Headers": headers,
            "Attachments": [],
        },
        message_id,
    )


@router.post("/demo/api/simulate-reply", response_model=None)
def post_simulate_reply(
    body: SimulateReplyIn,
    _admin: AdminDep,
    settings: SettingsDep,
    session: SessionDep,
    classifier: ClassifierDep,
    gateway: OptionalGatewayDep,
    identity: OptionalIdentityDep,
    now: NowDep,
) -> Response:
    found = session.execute(
        select(Invoice, Customer)
        .join(Customer, Customer.id == Invoice.customer_id)
        .where(Invoice.id == body.invoice_id)
    ).one_or_none()
    if found is None:
        return _error("invoice_not_found", 404)
    invoice, customer = found
    last_sent = session.scalars(
        select(OutboundEmail)
        .where(OutboundEmail.invoice_id == invoice.id, OutboundEmail.status == OutboundStatus.SENT)
        .order_by(OutboundEmail.sent_at.desc().nulls_last(), OutboundEmail.created_at.desc())
        .limit(1)
    ).first()
    payload, message_id = build_reply_payload(invoice, customer, last_sent, body)

    # The same function the real webhook calls after it has checked the Basic credentials.
    response = process_inbound(session, payload, settings, classifier, gateway, identity, now)

    result: dict[str, Any] = json.loads(bytes(response.body))
    if response.status_code != 200:
        return JSONResponse(result, status_code=response.status_code)
    session.expire_all()
    message = session.scalars(
        select(InboundMessage).where(InboundMessage.message_id == message_id)
    ).one_or_none()
    if message is None:
        return _error("message_not_stored", 500)
    return JSONResponse(
        {
            "inbound_message_id": str(message.id),
            "route": message.route,
            "review_status": message.review_status,
            "intent": message.intent,
            "intent_confidence": message.intent_confidence,
        }
    )


# --- what the log sender "sent", and the sample policy -------------------------------------------


@router.get("/demo/api/sent-emails", response_model=None)
def get_sent_emails(_admin: AdminDep, session: SessionDep) -> Response:
    rows = session.scalars(
        select(DemoSentEmail).order_by(DemoSentEmail.created_at.desc(), DemoSentEmail.id.desc())
    ).all()
    return JSONResponse(
        [
            {
                "id": str(r.id),
                "created_at": r.created_at.isoformat(),
                "to_address": r.to_address,
                "subject": r.subject,
                "body_text": r.body_text,
                "attachment_names": r.attachment_names,
                "provider_message_id": r.provider_message_id,
            }
            for r in rows
        ]
    )


@router.get("/demo/api/sample-policy", response_model=None)
def get_sample_policy(_admin: AdminDep) -> Response:
    try:
        text = SAMPLE_POLICY.read_text(encoding="utf-8")
    except OSError:
        return _error("sample_policy_unavailable", 404)
    return JSONResponse({"title": "Credit control policy", "text": text})
