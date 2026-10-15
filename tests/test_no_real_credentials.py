"""The test session must never pick up a project `.env` or real-service environment."""

import os
from pathlib import Path

import pytest

from collection_agent.settings import Settings, TypesafeSettings
from tests.api_helpers import make_settings

DB = "postgresql+psycopg://x:x@127.0.0.1:1/x"


def test_a_dotenv_file_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "COGNEE_API_KEY=should-not-be-read\nTYPESAFE_API_KEY=should-not-be-read\n"
    )
    monkeypatch.chdir(tmp_path)

    assert make_settings().cognee_api_key is None
    bare = Settings(database_url=DB)
    assert bare.cognee_api_key is None
    assert bare.typesafe_api_key is None
    assert TypesafeSettings().typesafe_api_key is None


def test_real_service_variables_are_unset_for_the_session() -> None:
    prefixes = ("COGNEE_", "STRIPE_", "POSTMARK_", "INBOUND_WEBHOOK_")
    exact = (
        "TYPESAFE_API_KEY",
        "ANTHROPIC_API_KEY",
        "ADMIN_API_TOKEN",
        "DEMO_MODE",
        "EMAIL_SENDER",
    )
    leaked = [n for n in os.environ if n.startswith(prefixes) or n in exact]
    assert leaked == []
