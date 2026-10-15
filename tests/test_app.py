from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from collection_agent.app import create_app, get_session
from collection_agent.db.engine import make_engine, make_session_factory


def _client_for(url_engine: Engine) -> TestClient:
    factory = make_session_factory(url_engine)

    def override() -> Iterator[Session]:
        with factory() as s:
            yield s

    app = create_app()
    app.dependency_overrides[get_session] = override
    return TestClient(app)


@pytest.mark.db
def test_healthz_ok(db_engine: Engine) -> None:
    resp = _client_for(db_engine).get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_healthz_503_when_db_unreachable() -> None:
    # Nothing listens on port 1, so the connection is refused immediately.
    engine = make_engine("postgresql+psycopg://x:x@127.0.0.1:1/x")
    resp = _client_for(engine).get("/healthz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "db_unavailable"}
