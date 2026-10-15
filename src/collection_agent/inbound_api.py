"""POST /webhooks/inbound (Postmark) and the review inbox.

The inbound webhook authenticates, dedupes and stores the message first, commits, and only
then classifies and routes. Anything that goes wrong after the commit leaves the message stored
and flagged for a human, and still answers 200 so Postmark does not retry and repeat the work.
"""

import base64
import binascii
import logging
import secrets
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Header, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import func, literal, select, update
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from collection_agent.calendar import uk_calendar
from collection_agent.chasing import InvoiceStatus
from collection_agent.classifier import (
    Classification,
    ClassifierFacts,
    Intent,
    ReplyClassifier,
)
from collection_agent.db.models import (
    Customer,
    InboundMessage,
    Invoice,
    OutboundEmail,
    OutboundStatus,
)
from collection_agent.db.repository import (
    NoApprovedPolicyError,
    get_approved_policy,
    record_inbound_message,
)
from collection_agent.deps import (
    LONDON,
    AdminDep,
    ClassifierDep,
    EmailIdentity,
    NowDep,
    OptionalGatewayDep,
    OptionalIdentityDep,
    SessionDep,
    SettingsDep,
)
from collection_agent.inbound import (
    MAX_STORED_BODY_CHARS,
    InboundEmail,
    extract_reply_text,
    is_auto_reply,
    parse_inbound,
)
from collection_agent.policy import ChasePolicy
from collection_agent.routing import route_reply
from collection_agent.settings import Settings
from collection_agent.stripe_gateway import StripeGateway
from collection_agent.templates import format_amount

logger = logging.getLogger(__name__)

router = APIRouter()

MIN_PROVIDER_ID_CHARS = 8  # ignore absurdly short ids so a substring match cannot be trivial


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


# --- authentication ----------------------------------------------------------------------------


def _basic_credentials(authorization: str | None) -> tuple[bytes, bytes] | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(token.strip(), validate=True)
    except binascii.Error, ValueError:
        return None
    user, sep, password = decoded.partition(b":")
    return (user, password) if sep else None


def _authenticated(settings: Settings, authorization: str | None) -> bool:
    expected_user, expected_password = (
        (settings.inbound_webhook_user or "").encode(),
        (settings.inbound_webhook_password or "").encode(),
    )
    given = _basic_credentials(authorization) or (b"", b"")
    # Evaluate both comparisons so timing does not say which one failed.
    user_ok = secrets.compare_digest(given[0], expected_user)
    password_ok = secrets.compare_digest(given[1], expected_password)
    return user_ok and password_ok


# --- matching ----------------------------------------------------------------------------------


def find_invoice(session: Session, email: InboundEmail) -> tuple[Invoice | None, str]:
    """Match a reply to an invoice; first hit wins. Ambiguity is never guessed at."""
    if email.mailbox_hashes:
        found = session.scalars(
            select(Invoice).where(Invoice.reply_token.in_(email.mailbox_hashes))
        ).all()
        if len(found) == 1:
            return found[0], "reply_token"

    threading_text = email.threading_text
    if threading_text:
        invoice_ids = session.scalars(
            select(OutboundEmail.invoice_id)
            .where(
                OutboundEmail.status == OutboundStatus.SENT,
                OutboundEmail.provider_message_id.is_not(None),
                func.char_length(func.coalesce(OutboundEmail.provider_message_id, ""))
                >= MIN_PROVIDER_ID_CHARS,
                func.strpos(literal(threading_text), OutboundEmail.provider_message_id) > 0,
            )
            .distinct()
        ).all()
        if len(invoice_ids) == 1:
            matched = session.get(Invoice, invoice_ids[0])
            if matched is not None:
                return matched, "in_reply_to"

    candidates = session.scalars(
        select(Invoice)
        .join(Customer, Customer.id == Invoice.customer_id)
        .where(
            func.lower(Customer.email) == email.from_address,
            Invoice.status == InvoiceStatus.OPEN,
            Invoice.sent_on.is_not(None),
        )
    ).all()
    if len(candidates) == 1:
        return candidates[0], "sender"
    return None, "none"


# --- the webhook -------------------------------------------------------------------------------


@router.post("/webhooks/inbound", response_model=None)
async def inbound_webhook(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    classifier: ClassifierDep,
    gateway: OptionalGatewayDep,
    identity: OptionalIdentityDep,
    now: NowDep,
    authorization: Annotated[str | None, Header()] = None,
) -> Response:
    if not settings.inbound_webhook_user or not settings.inbound_webhook_password:
        return _error("inbound_webhook_not_configured", 503)
    if not _authenticated(settings, authorization):
        return JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="inbound"'},
        )
    try:
        payload = await request.json()
    except ValueError:
        return _error("invalid_json", 400)
    return await run_in_threadpool(
        _process, session, payload, settings, classifier, gateway, identity, now
    )


def _process(
    session: Session,
    payload: Any,
    settings: Settings,
    classifier: ReplyClassifier | None,
    gateway: StripeGateway | None,
    identity: EmailIdentity | None,
    now: datetime,
) -> Response:
    email = parse_inbound(payload)
    if email is None:
        return _error("no_message_id", 400)

    # Phase 1: authenticate-then-store. Any failure here is a 500 so Postmark retries; the
    # dedupe key makes the retry safe.
    try:
        invoice, method = find_invoice(session, email)
        auto = is_auto_reply(email.headers, email.subject)
        text = extract_reply_text(email.stripped_reply, email.text_body)
        if auto:
            route, review, counts = "auto_reply_ignored", "not_needed", False
        elif invoice is None:
            route, review, counts = "unmatched", "needs_review", True
        elif not text:
            route, review, counts = "empty_body", "needs_review", True
        else:
            # A safe default if the process dies before routing finishes: a human looks.
            route, review, counts = "classification_pending", "needs_review", True
        message_pk = uuid.uuid4()
        inserted = record_inbound_message(
            session,
            id=message_pk,
            invoice_id=invoice.id if invoice else None,
            message_id=email.message_id,
            from_address=email.from_address,
            subject=email.subject,
            body_text=email.text_body[:MAX_STORED_BODY_CHARS],
            received_at=now,
            counts_as_response=counts,
            postmark_message_id=email.postmark_message_id,
            match_method=method,
            is_auto_reply=auto,
            classified_text=text or None,
            references_header=email.headers.get("references") or None,
            route=route,
            review_status=review,
            attachment_count=email.attachment_count,
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        # The type only: a database error's text can carry the customer's email as parameters.
        logger.error("inbound webhook: storing the message failed: %s", type(exc).__name__)
        return _error("storing_failed", 500)
    if not inserted:
        return JSONResponse({"status": "duplicate"}, status_code=200)
    if route != "classification_pending" or invoice is None:
        return JSONResponse({"status": "ok", "route": route}, status_code=200)

    # Phase 2: classify and route. Failures leave the stored message flagged for review.
    try:
        _classify_and_route(
            session, message_pk, invoice, text, classifier, gateway, identity, settings, now
        )
    except Exception as exc:
        session.rollback()
        logger.error("inbound webhook: classification or routing failed: %s", type(exc).__name__)
        _mark_failed(session, message_pk)
        return JSONResponse({"status": "ok", "route": "classification_failed"}, status_code=200)
    return JSONResponse({"status": "ok"}, status_code=200)


def _classify_and_route(
    session: Session,
    message_pk: uuid.UUID,
    invoice: Invoice,
    text: str,
    classifier: ReplyClassifier | None,
    gateway: StripeGateway | None,
    identity: EmailIdentity | None,
    settings: Settings,
    now: datetime,
) -> None:
    if classifier is None:
        raise RuntimeError("classifier not configured")
    today = now.astimezone(LONDON).date()
    facts = ClassifierFacts(
        reference=invoice.reference,
        amount=format_amount(invoice.amount_minor, invoice.currency),
        due_on=invoice.due_on,
        today=today,
    )
    classification = classifier.classify(text, facts)  # network; no transaction is held open

    message = session.get(InboundMessage, message_pk)
    assert message is not None
    _record_classification(message, classification)
    session.commit()  # the classification is kept even if routing then fails

    message = session.get(InboundMessage, message_pk)
    assert message is not None
    route_reply(
        session,
        message,
        classification,
        invoice,
        _approved_policy(session),
        uk_calendar([today.year, today.year + 1]),
        gateway,
        identity,
        today,
        now=now,
        min_confidence=settings.classifier_min_confidence,
    )
    session.commit()


def _record_classification(message: InboundMessage, c: Classification) -> None:
    message.intent = c.intent.value
    message.intent_confidence = c.confidence
    message.intent_probabilities = c.probabilities
    message.classifier_model = c.model
    message.promised_date_raw = c.promised_date_parts or None


def _approved_policy(session: Session) -> ChasePolicy | None:
    try:
        return get_approved_policy(session)
    except NoApprovedPolicyError:
        return None


def _mark_failed(session: Session, message_pk: uuid.UUID) -> None:
    try:
        session.execute(
            update(InboundMessage)
            .where(InboundMessage.id == message_pk)
            .values(route="classification_failed", review_status="needs_review")
        )
        session.commit()
    except Exception as exc:
        session.rollback()
        logger.error("inbound webhook: could not mark the message failed: %s", type(exc).__name__)


# --- the review inbox ----------------------------------------------------------------------------

ReviewStatus = Literal["not_needed", "needs_review", "reviewed"]
LIST_TEXT_CHARS = 500


class InboxItem(BaseModel):
    id: uuid.UUID
    received_at: datetime
    from_address: str
    subject: str
    classified_text: str | None
    intent: str | None
    intent_confidence: float | None
    route: str | None
    match_method: str
    invoice_id: uuid.UUID | None
    invoice_reference: str | None
    review_status: str
    drafted_outbox_ids: list[uuid.UUID]


class InboxDetail(InboxItem):
    body_text: str
    intent_probabilities: dict[str, float] | None
    classifier_model: str | None
    promised_date: date | None
    promised_date_raw: dict[str, str] | None
    is_auto_reply: bool
    counts_as_response: bool
    attachment_count: int
    postmark_message_id: str | None
    reviewed_by: str | None
    reviewed_at: datetime | None
    reviewer_intent: str | None
    review_note: str | None


def _drafted_ids(session: Session, message_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        session.scalars(
            select(OutboundEmail.id)
            .where(OutboundEmail.inbound_message_id == message_id)
            .order_by(OutboundEmail.created_at, OutboundEmail.id)
        )
    )


def _reference(session: Session, invoice_id: uuid.UUID | None) -> str | None:
    if invoice_id is None:
        return None
    return session.scalar(select(Invoice.reference).where(Invoice.id == invoice_id))


def _item_fields(session: Session, m: InboundMessage, *, truncate: bool) -> dict[str, Any]:
    text = m.classified_text
    return {
        "id": m.id,
        "received_at": m.received_at,
        "from_address": m.from_address,
        "subject": m.subject,
        "classified_text": text[:LIST_TEXT_CHARS] if text and truncate else text,
        "intent": m.intent,
        "intent_confidence": m.intent_confidence,
        "route": m.route,
        "match_method": m.match_method,
        "invoice_id": m.invoice_id,
        "invoice_reference": _reference(session, m.invoice_id),
        "review_status": m.review_status,
        "drafted_outbox_ids": _drafted_ids(session, m.id),
    }


def _detail(session: Session, m: InboundMessage) -> InboxDetail:
    return InboxDetail(
        **_item_fields(session, m, truncate=False),
        body_text=m.body_text,
        intent_probabilities=m.intent_probabilities,
        classifier_model=m.classifier_model,
        promised_date=m.promised_date,
        promised_date_raw=m.promised_date_raw,
        is_auto_reply=m.is_auto_reply,
        counts_as_response=m.counts_as_response,
        attachment_count=m.attachment_count,
        postmark_message_id=m.postmark_message_id,
        reviewed_by=m.reviewed_by,
        reviewed_at=m.reviewed_at,
        reviewer_intent=m.reviewer_intent,
        review_note=m.review_note,
    )


@router.get("/inbox", response_model=None)
def list_inbox(
    _admin: AdminDep,
    session: SessionDep,
    review_status: Annotated[ReviewStatus, Query()] = "needs_review",
) -> Response:
    rows = session.scalars(
        select(InboundMessage)
        .where(InboundMessage.review_status == review_status)
        .order_by(InboundMessage.received_at.desc(), InboundMessage.id.desc())
    ).all()
    return JSONResponse(
        [InboxItem(**_item_fields(session, m, truncate=True)).model_dump(mode="json") for m in rows]
    )


@router.get("/inbox/{message_id}", response_model=None)
def get_inbox_message(message_id: uuid.UUID, _admin: AdminDep, session: SessionDep) -> Response:
    message = session.get(InboundMessage, message_id)
    if message is None:
        return _error("not_found", 404)
    return JSONResponse(_detail(session, message).model_dump(mode="json"))


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewed_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    reviewer_intent: Intent | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)] | None = None
    invoice_id: uuid.UUID | None = None


@router.post("/inbox/{message_id}/review", response_model=None)
def review_message(
    message_id: uuid.UUID, body: ReviewIn, _admin: AdminDep, session: SessionDep, now: NowDep
) -> Response:
    """Log what a person did about a message. This changes no invoice and sends nothing; for an
    unmatched message it may link the invoice the person found, without re-routing."""
    message = session.scalars(
        select(InboundMessage)
        .where(InboundMessage.id == message_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if message is None:
        session.rollback()
        return _error("not_found", 404)
    if message.review_status == "reviewed":
        session.rollback()
        return _error("already_reviewed", 409)
    if body.invoice_id is not None:
        if message.invoice_id is not None:
            session.rollback()
            return _error("already_matched", 422)
        if session.get(Invoice, body.invoice_id) is None:
            session.rollback()
            return _error("invoice_not_found", 404)
        message.invoice_id = body.invoice_id
    message.review_status = "reviewed"
    message.reviewed_by = body.reviewed_by
    message.reviewed_at = now
    message.reviewer_intent = body.reviewer_intent.value if body.reviewer_intent else None
    message.review_note = body.note or None
    session.commit()
    return JSONResponse(_detail(session, message).model_dump(mode="json"))
