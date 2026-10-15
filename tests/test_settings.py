import pytest

from collection_agent.settings import Settings


def test_database_url_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost/db")
    assert Settings().database_url == "postgresql+psycopg://u:p@localhost/db"
