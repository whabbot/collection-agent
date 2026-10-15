"""Policy text in, rule cards out. Shared by the API and the CLI.

Order: store the text, send it to Cognee (soft guidance and the graph), then have Claude propose
a draft `ChasePolicy`. Cognee failing never blocks the draft; Claude failing never loses the text.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from collection_agent.cognee_client import CogneeError, CogneeGateway
from collection_agent.db.models import ChasePolicyRow, PolicyDocument, PolicyStatus
from collection_agent.db.repository import (
    add_policy_document,
    add_policy_extraction,
    create_draft_policy,
    get_approved_policy_row,
    get_policy_extraction,
    policy_from_row,
)
from collection_agent.policy import ChasePolicy
from collection_agent.policy_cards import Card, build_cards
from collection_agent.policy_extraction import (
    PolicyExtractor,
    validate_extraction,
)


@dataclass(frozen=True)
class CardSet:
    version: int
    status: PolicyStatus
    cards: list[Card]
    soft_guidance: list[dict[str, str]]


def store_and_ingest(
    session: Session, title: str, text: str, cognee: CogneeGateway | None, dataset: str
) -> PolicyDocument:
    doc = add_policy_document(session, title, text)
    if cognee is None:
        doc.cognee_status = "not_configured"
    else:
        try:
            cognee.add_text([text], dataset)
            cognee.cognify(dataset)
        except CogneeError as exc:
            doc.cognee_status = "failed"
            doc.cognee_error = str(exc)[:200]
        else:
            doc.cognee_status = "ingested"
    session.flush()
    return doc


def extract_and_draft(session: Session, doc: PolicyDocument, extractor: PolicyExtractor) -> int:
    """Raises ExtractionError (nothing written). Returns the new draft's version."""
    approved_row = get_approved_policy_row(session)
    fallback = policy_from_row(approved_row) if approved_row is not None else ChasePolicy()
    proposed = validate_extraction(extractor.extract(doc.text), doc.text, fallback)
    version = create_draft_policy(session, proposed.policy)
    add_policy_extraction(
        session,
        document_id=doc.id,
        version=version,
        model=extractor.model,
        fields={name: r.to_json() for name, r in proposed.fields.items()},
        soft_guidance=[{"quote": g.quote, "summary": g.summary} for g in proposed.soft_guidance],
    )
    return version


def cards_for(session: Session, row: ChasePolicyRow) -> CardSet:
    extraction = get_policy_extraction(session, row.version)
    approved_row = get_approved_policy_row(session)
    cards = build_cards(
        policy_from_row(row),
        extraction.fields if extraction is not None else None,
        policy_from_row(approved_row) if approved_row is not None else None,
    )
    guidance = [dict(g) for g in extraction.soft_guidance] if extraction is not None else []
    return CardSet(row.version, row.status, cards, guidance)
