"""Policy endpoints (admin bearer): paste text, review rule cards, approve, see the graph."""

from typing import Annotated, Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from collection_agent.cognee_client import CogneeError
from collection_agent.db.models import PolicyStatus
from collection_agent.db.repository import (
    PolicyStateError,
    approve_policy,
    get_approved_policy_row,
    get_policy_row,
)
from collection_agent.deps import (
    AdminDep,
    CogneeDep,
    ExtractorDep,
    SessionDep,
    SettingsDep,
)
from collection_agent.policy_extraction import ExtractionError
from collection_agent.policy_ingest import CardSet, cards_for, extract_and_draft, store_and_ingest

router = APIRouter(prefix="/policies")

MIN_POLICY_CHARS = 50
MAX_POLICY_CHARS = 50_000
GRAPH_CSP = "sandbox allow-scripts"


class DocumentIn(BaseModel):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    text: Annotated[str, Field(min_length=MIN_POLICY_CHARS, max_length=MAX_POLICY_CHARS)]


class ApproveIn(BaseModel):
    approved_by: Annotated[str, Field(min_length=1, max_length=200)]


def _cards_body(card_set: CardSet) -> dict[str, Any]:
    return {
        "policy_version": card_set.version,
        "status": card_set.status.value,
        "cards": [c.to_dict() for c in card_set.cards],
        "soft_guidance": card_set.soft_guidance,
    }


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": {"error": "policy_not_found"}})


@router.post("/documents", status_code=201)
def post_document(
    body: DocumentIn,
    _admin: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    cognee: CogneeDep,
    extractor: ExtractorDep,
) -> Response:
    doc = store_and_ingest(session, body.title, body.text, cognee, settings.cognee_policies_dataset)
    cognee_status = doc.cognee_status
    document_id = str(doc.id)
    if extractor is None:
        session.commit()  # the text stays stored
        return JSONResponse(status_code=503, content={"error": "claude_not_configured"})
    try:
        version = extract_and_draft(session, doc, extractor)
    except ExtractionError:
        session.commit()
        return JSONResponse(status_code=502, content={"error": "extraction_failed"})
    row = get_policy_row(session, version)
    assert row is not None
    card_set = cards_for(session, row)
    session.commit()
    return JSONResponse(
        status_code=201,
        content={
            "document_id": document_id,
            "cognee_status": cognee_status,
            "policy_version": version,
            "cards": [c.to_dict() for c in card_set.cards],
            "soft_guidance": card_set.soft_guidance,
        },
    )


@router.get("/approved")
def get_approved(_admin: AdminDep, session: SessionDep) -> Response:
    row = get_approved_policy_row(session)
    if row is None:
        return _not_found()
    return JSONResponse(content=_cards_body(cards_for(session, row)))


@router.get("/graph")
def get_graph(_admin: AdminDep, settings: SettingsDep, cognee: CogneeDep) -> Response:
    if cognee is None:
        return JSONResponse(status_code=503, content={"error": "cognee_not_configured"})
    try:
        dataset_id = cognee.dataset_id(settings.cognee_policies_dataset)
        if dataset_id is None:
            return JSONResponse(status_code=404, content={"error": "dataset_not_found"})
        html = cognee.visualize_html(dataset_id)
    except CogneeError:
        return JSONResponse(status_code=502, content={"error": "cognee_failed"})
    return Response(
        content=html, media_type="text/html", headers={"Content-Security-Policy": GRAPH_CSP}
    )


@router.get("/{version}/cards")
def get_cards(version: int, _admin: AdminDep, session: SessionDep) -> Response:
    row = get_policy_row(session, version)
    if row is None:
        return _not_found()
    return JSONResponse(content=_cards_body(cards_for(session, row)))


@router.post("/{version}/approve")
def post_approve(version: int, body: ApproveIn, _admin: AdminDep, session: SessionDep) -> Response:
    row = get_policy_row(session, version)
    if row is None:
        return _not_found()
    status_before = PolicyStatus(row.status).value
    try:
        approve_policy(session, version, body.approved_by)
    except PolicyStateError:
        session.rollback()
        return JSONResponse(
            status_code=409,
            content={"error": "not_a_draft", "status": status_before},
        )
    session.commit()
    row = get_policy_row(session, version)
    assert row is not None
    return JSONResponse(content=_cards_body(cards_for(session, row)))
