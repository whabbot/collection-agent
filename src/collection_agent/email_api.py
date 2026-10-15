"""Contract upload, invoice-email drafting and the approval outbox."""

import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from collection_agent.chasing import InvoiceState, InvoiceStatus
from collection_agent.db.models import (
    Contract,
    Customer,
    Invoice,
    OutboundEmail,
    OutboundKind,
    OutboundStatus,
)
from collection_agent.deps import AdminDep, EmailIdentityDep, EmailSenderDep, SessionDep, TodayDep
from collection_agent.email_sender import Attachment, EmailSendError, OutgoingEmail
from collection_agent.templates import render_invoice_email

logger = logging.getLogger(__name__)

router = APIRouter()

# Base64 inflates by ~37% and Postmark caps a whole message at 10 MB.
MAX_CONTRACT_BYTES = 7 * 1024 * 1024
_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()-]{0,95}\.pdf$", re.IGNORECASE)
_DEAD = (OutboundStatus.REJECTED, OutboundStatus.FAILED)


def _error(code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status)


def _invoice_is_open(inv: Invoice) -> bool:
    return inv.status is InvoiceStatus.OPEN and inv.state is InvoiceState.ACTIVE


# --- contract upload ---------------------------------------------------------------------


async def _read_limited(request: Request) -> bytes | None:
    """The request body, or None if it is larger than MAX_CONTRACT_BYTES."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_CONTRACT_BYTES:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_CONTRACT_BYTES:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.put("/invoices/{invoice_id}/contract", response_model=None)
async def put_contract(
    invoice_id: uuid.UUID,
    request: Request,
    _admin: AdminDep,
    session: SessionDep,
    filename: Annotated[str, Query(min_length=1, max_length=100)],
) -> Response:
    if not _FILENAME_RE.match(filename):
        return _error("invalid_filename", 422)
    content = await _read_limited(request)
    if content is None:
        return _error("contract_too_large", 422)
    if not content:
        return _error("empty_body", 422)
    if not content.startswith(b"%PDF-"):
        return _error("not_a_pdf", 422)
    return await run_in_threadpool(_store_contract, session, invoice_id, filename, content)


def _store_contract(
    session: Session, invoice_id: uuid.UUID, filename: str, content: bytes
) -> Response:
    if session.get(Invoice, invoice_id) is None:
        return _error("invoice_not_found", 404)
    if _has_email_in_flight_or_sent(session, invoice_id):
        return _error("invoice_email_already_sent", 409)
    sha256 = hashlib.sha256(content).hexdigest()
    values = {
        "filename": filename,
        "content": content,
        "sha256": sha256,
        "size_bytes": len(content),
    }
    session.execute(
        pg_insert(Contract)
        .values(id=uuid.uuid4(), invoice_id=invoice_id, **values)
        .on_conflict_do_update(index_elements=["invoice_id"], set_=values)
    )
    session.commit()
    return JSONResponse({"filename": filename, "size_bytes": len(content), "sha256": sha256})


def _has_email_in_flight_or_sent(session: Session, invoice_id: uuid.UUID) -> bool:
    # `sending` counts: the message may already be out, so the contract must stay as it was.
    return (
        session.scalars(
            select(OutboundEmail.id).where(
                OutboundEmail.invoice_id == invoice_id,
                OutboundEmail.kind == OutboundKind.INVOICE,
                OutboundEmail.status.in_([OutboundStatus.SENT, OutboundStatus.SENDING]),
            )
        ).first()
        is not None
    )


# --- outbox views --------------------------------------------------------------------------


class ContractInfo(BaseModel):
    filename: str
    size_bytes: int
    sha256: str


class OutboxOut(BaseModel):
    id: uuid.UUID
    invoice_id: uuid.UUID
    kind: str
    status: str
    created_at: datetime
    updated_at: datetime
    to_address: str
    from_address: str
    from_name: str
    reply_to: str
    subject: str
    body_text: str
    attach_contract: bool
    contract: ContractInfo | None
    approved_by: str | None
    approved_at: datetime | None
    rejected_by: str | None
    rejected_at: datetime | None
    reject_reason: str | None
    sent_at: datetime | None
    provider_message_id: str | None
    error: str | None


def _contract_info(session: Session, row: OutboundEmail) -> ContractInfo | None:
    if not row.attach_contract:
        return None
    found = session.execute(
        select(Contract.filename, Contract.size_bytes, Contract.sha256).where(
            Contract.invoice_id == row.invoice_id
        )
    ).one_or_none()
    return (
        None
        if found is None
        else ContractInfo(filename=found.filename, size_bytes=found.size_bytes, sha256=found.sha256)
    )


def _out(session: Session, row: OutboundEmail) -> OutboxOut:
    return OutboxOut(
        id=row.id,
        invoice_id=row.invoice_id,
        kind=row.kind,
        status=row.status.value,
        created_at=row.created_at,
        updated_at=row.updated_at,
        to_address=row.to_address,
        from_address=row.from_address,
        from_name=row.from_name,
        reply_to=row.reply_to,
        subject=row.subject,
        body_text=row.body_text,
        attach_contract=row.attach_contract,
        contract=_contract_info(session, row),
        approved_by=row.approved_by,
        approved_at=row.approved_at,
        rejected_by=row.rejected_by,
        rejected_at=row.rejected_at,
        reject_reason=row.reject_reason,
        sent_at=row.sent_at,
        provider_message_id=row.provider_message_id,
        error=row.error,
    )


def _json(session: Session, row: OutboundEmail, status: int = 200) -> JSONResponse:
    return JSONResponse(_out(session, row).model_dump(mode="json"), status_code=status)


# --- drafting --------------------------------------------------------------------------------


@router.post("/invoices/{invoice_id}/invoice-email", response_model=None, status_code=201)
def draft_invoice_email(
    invoice_id: uuid.UUID, _admin: AdminDep, identity: EmailIdentityDep, session: SessionDep
) -> Response:
    row = session.execute(
        select(Invoice, Customer)
        .join(Customer, Customer.id == Invoice.customer_id)
        .where(Invoice.id == invoice_id)
    ).one_or_none()
    if row is None:
        return _error("invoice_not_found", 404)
    invoice, customer = row

    has_contract = (
        session.scalars(select(Contract.id).where(Contract.invoice_id == invoice_id)).first()
        is not None
    )
    if not has_contract:
        return _error("contract_missing", 409)
    if not _invoice_is_open(invoice):
        return _error("invoice_not_open", 409)
    live = session.scalars(
        select(OutboundEmail.id).where(
            OutboundEmail.invoice_id == invoice_id,
            OutboundEmail.kind == OutboundKind.INVOICE,
            OutboundEmail.status.not_in(_DEAD),
        )
    ).first()
    if live is not None:
        return _error("already_drafted", 409)
    if not invoice.hosted_invoice_url:
        return _error("invoice_url_missing", 409)

    subject, body = render_invoice_email(
        reference=invoice.reference,
        customer_name=customer.name,
        amount_minor=invoice.amount_minor,
        currency=invoice.currency,
        due_on=invoice.due_on,
        hosted_invoice_url=invoice.hosted_invoice_url,
        contract_id=invoice.contract_id,
        from_name=identity.from_name,
        company_name=identity.company_name,
    )
    email = OutboundEmail(
        invoice_id=invoice_id,
        kind=OutboundKind.INVOICE.value,
        to_address=customer.email,
        from_address=identity.from_address,
        from_name=identity.from_name,
        reply_to=f"reply+{invoice.reply_token}@{identity.reply_to_domain}",
        subject=subject,
        body_text=body,
        attach_contract=True,
        status=OutboundStatus.PENDING_APPROVAL,
    )
    session.add(email)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # a concurrent draft won the partial unique index
        return _error("already_drafted", 409)
    return _json(session, email, 201)


# --- outbox ------------------------------------------------------------------------------------


@router.get("/outbox", response_model=None)
def list_outbox(
    _admin: AdminDep,
    session: SessionDep,
    status: OutboundStatus = OutboundStatus.PENDING_APPROVAL,
) -> Response:
    rows = session.scalars(
        select(OutboundEmail)
        .where(OutboundEmail.status == status)
        .order_by(OutboundEmail.created_at.desc(), OutboundEmail.id.desc())
    ).all()
    return JSONResponse([_out(session, r).model_dump(mode="json") for r in rows])


@router.get("/outbox/{outbox_id}", response_model=None)
def get_outbox(outbox_id: uuid.UUID, _admin: AdminDep, session: SessionDep) -> Response:
    row = session.get(OutboundEmail, outbox_id)
    if row is None:
        return _error("not_found", 404)
    return _json(session, row)


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approved_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]


class RejectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rejected_by: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
    ]
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


def _lock_outbox(session: Session, outbox_id: uuid.UUID) -> OutboundEmail | None:
    return session.scalars(
        select(OutboundEmail)
        .where(OutboundEmail.id == outbox_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()


@router.post("/outbox/{outbox_id}/reject", response_model=None)
def reject(outbox_id: uuid.UUID, body: RejectIn, _admin: AdminDep, session: SessionDep) -> Response:
    row = _lock_outbox(session, outbox_id)
    if row is None:
        session.rollback()
        return _error("not_found", 404)
    if row.status is not OutboundStatus.PENDING_APPROVAL:
        session.rollback()
        return _error("not_pending", 409)
    row.status = OutboundStatus.REJECTED
    row.rejected_by = body.rejected_by
    row.rejected_at = datetime.now(UTC)
    row.reject_reason = body.reason
    session.commit()
    return _json(session, row)


@router.post("/outbox/{outbox_id}/approve", response_model=None)
def approve(
    outbox_id: uuid.UUID,
    body: ApproveIn,
    _admin: AdminDep,
    sender: EmailSenderDep,  # resolved first: a missing Postmark token must change no state
    session: SessionDep,
    today: TodayDep,
) -> Response:
    # Step 1: claim the row. `sending` is committed before the network call so that a second
    # approval (or a crash) can never produce a second send.
    row = _lock_outbox(session, outbox_id)
    if row is None:
        session.rollback()
        return _error("not_found", 404)
    if row.status is not OutboundStatus.PENDING_APPROVAL:
        session.rollback()
        return _error("not_pending", 409)

    invoice = session.scalars(
        select(Invoice)
        .where(Invoice.id == row.invoice_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    if not _invoice_is_open(invoice):
        row.status = OutboundStatus.REJECTED
        row.rejected_by = "system"
        row.rejected_at = datetime.now(UTC)
        row.reject_reason = "invoice no longer open"
        session.commit()
        return _error("invoice_not_open", 409)

    attachments: tuple[Attachment, ...] = ()
    if row.attach_contract:
        contract = session.scalars(
            select(Contract).where(Contract.invoice_id == row.invoice_id)
        ).one_or_none()
        if contract is None:
            session.rollback()
            return _error("contract_missing", 409)
        attachments = (Attachment(contract.filename, contract.content, "application/pdf"),)

    message = OutgoingEmail(
        from_name=row.from_name,
        from_address=row.from_address,
        to=row.to_address,
        reply_to=row.reply_to,
        subject=row.subject,
        text_body=row.body_text,
        attachments=attachments,
        metadata={"invoice_id": str(row.invoice_id), "outbound_email_id": str(row.id)},
    )
    row.status = OutboundStatus.SENDING
    row.approved_by = body.approved_by
    row.approved_at = datetime.now(UTC)
    session.commit()

    # Step 2: send. If the process dies after this point and before step 3 the row stays in
    # `sending` and is NEVER re-sent automatically, because the message may already have gone
    # out. A person checks Postmark's activity log and resolves it by hand.
    try:
        sent = sender.send(message)
    except EmailSendError as exc:
        logger.error(
            "invoice email send failed: outbox=%s code=%s unknown_outcome=%s",
            row.id,
            exc.error_code,
            exc.outcome_unknown,
        )
        if exc.outcome_unknown:
            # Might have been delivered: leave it in `sending` for a person to check.
            row.error = str(exc)[:500]
            session.commit()
            return _error("send_outcome_unknown", 502)
        row.status = OutboundStatus.FAILED
        row.error = str(exc)[:500]
        session.commit()
        return _error("send_failed", 502)

    # Step 3: record the send.
    row.status = OutboundStatus.SENT
    row.sent_at = datetime.now(UTC)
    row.provider_message_id = sent.provider_message_id
    if row.kind == OutboundKind.INVOICE and invoice.sent_on is None:
        invoice.sent_on = today
    session.commit()
    return _json(session, row)
