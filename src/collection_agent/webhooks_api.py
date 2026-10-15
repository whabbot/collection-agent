"""POST /webhooks/stripe: verify, dedupe, apply."""

import logging

from fastapi import APIRouter, Header, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import Invoice, StripeEvent
from collection_agent.deps import SessionDep, SettingsDep
from collection_agent.stripe_gateway import VerifiedEvent, WebhookSignatureError, verify_webhook

logger = logging.getLogger(__name__)

router = APIRouter()


def _apply(session: Session, event: VerifiedEvent) -> None:
    """Apply an event to our invoice. Terminal statuses are never moved backwards: every
    update is conditional on the invoice still being open, so out-of-order delivery is safe.
    Anything not listed is recorded only (the scheduler owns chasing)."""
    if event.object_id is None:
        return
    match event.type:
        case "invoice.paid":
            _set_status(session, event.object_id, InvoiceStatus.PAID)
        case "invoice.voided":
            _set_status(session, event.object_id, InvoiceStatus.VOID)
        case "invoice.marked_uncollectible":
            session.execute(
                update(Invoice)
                .where(
                    Invoice.stripe_invoice_id == event.object_id,
                    Invoice.status == InvoiceStatus.OPEN,
                    Invoice.state == InvoiceState.ACTIVE,
                )
                .values(state=InvoiceState.WITH_HUMAN)
            )
        case _:
            pass


def _set_status(session: Session, stripe_invoice_id: str, status: InvoiceStatus) -> None:
    session.execute(
        update(Invoice)
        .where(Invoice.stripe_invoice_id == stripe_invoice_id, Invoice.status == InvoiceStatus.OPEN)
        .values(status=status)
    )


@router.post("/webhooks/stripe", response_model=None)
async def stripe_webhook(
    request: Request,
    settings: SettingsDep,
    session: SessionDep,
    stripe_signature: str | None = Header(default=None),
) -> Response:
    secret = settings.stripe_webhook_secret
    if not secret:
        return JSONResponse({"error": "webhook_secret_not_configured"}, status_code=503)

    raw = await request.body()  # the raw bytes: the signature covers them exactly
    return await run_in_threadpool(_process, session, raw, stripe_signature, secret)


def _process(session: Session, raw: bytes, signature: str | None, secret: str) -> Response:
    """Verify, dedupe, apply and commit. Blocking, so it runs in a worker thread."""
    try:
        event = verify_webhook(raw, signature, secret)
    except WebhookSignatureError:
        return JSONResponse({"error": "invalid_signature"}, status_code=400)

    try:
        inserted = session.execute(
            pg_insert(StripeEvent)
            .values(
                event_id=event.id,
                type=event.type,
                stripe_object_id=event.object_id,
                livemode=event.livemode,
            )
            .on_conflict_do_nothing(index_elements=["event_id"])
            .returning(StripeEvent.id)
        ).scalar_one_or_none()
        if inserted is None:
            session.rollback()
            return JSONResponse({"status": "duplicate"}, status_code=200)
        _apply(session, event)
        session.commit()
    except Exception:
        session.rollback()
        logger.exception("stripe webhook processing failed: event=%s type=%s", event.id, event.type)
        return JSONResponse({"error": "processing_failed"}, status_code=500)
    return JSONResponse({"status": "ok"}, status_code=200)
