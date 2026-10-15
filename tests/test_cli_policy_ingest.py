from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.cli import main
from collection_agent.db.models import ChasePolicyRow, PolicyDocument
from collection_agent.policy_extraction import ExtractionError
from tests.api_helpers import make_settings
from tests.fake_policy import SAMPLE, FakeCognee, FakeExtractor

pytestmark = pytest.mark.db

FIXTURE = "tests/fixtures/policy_sample.txt"


def ingest(
    factory: sessionmaker[Session],
    *,
    cognee: FakeCognee | None = None,
    extractor: FakeExtractor | None = None,
    file: str = FIXTURE,
) -> int:
    return main(
        ["policy", "ingest", "--title", "Credit control", "--file", file],
        settings=make_settings(),
        session_factory=factory,
        cognee=cognee,
        extractor=extractor,
    )


def test_ingest_prints_the_cards_and_creates_a_draft(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    cognee = FakeCognee()

    code = ingest(session_factory, cognee=cognee, extractor=FakeExtractor())

    out = capsys.readouterr().out
    assert code == 0
    assert "(cognee: ingested)" in out
    assert "Policy version 1 (draft)" in out
    assert "Follow up after 7 days with no response" in out
    assert "Payment must clear by the 15th of the following month" in out
    assert 'from the document: "After three reminders, escalate to the finance manager"' in out
    assert "policy approve --version 1" in out
    assert cognee.added == [([SAMPLE], "policies")]
    assert session.scalars(select(ChasePolicyRow)).one().status.value == "draft"


def test_ingest_without_claude_keeps_the_document_and_exits_1(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    code = ingest(session_factory, extractor=None)

    captured = capsys.readouterr()
    assert code == 1
    assert "ANTHROPIC_API_KEY" in captured.err
    assert session.scalars(select(PolicyDocument)).one().text == SAMPLE
    assert session.scalars(select(ChasePolicyRow)).first() is None


def test_ingest_extraction_failure_exits_2_with_no_draft(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    code = ingest(session_factory, extractor=FakeExtractor(error=ExtractionError("declined")))

    assert code == 2
    assert "extraction failed" in capsys.readouterr().err
    assert session.scalars(select(PolicyDocument)).one()
    assert session.scalars(select(ChasePolicyRow)).first() is None


def test_ingest_rejects_a_missing_or_too_short_file(
    session_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    short = tmp_path / "short.txt"
    short.write_text("too short")
    assert ingest(session_factory, extractor=FakeExtractor(), file=str(short)) == 1
    assert ingest(session_factory, extractor=FakeExtractor(), file="/no/such/file.txt") == 1
