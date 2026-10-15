"""FastAPI application factory."""

from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, FastAPI, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.db.engine import make_engine, make_session_factory
from collection_agent.settings import get_settings


@lru_cache
def _session_factory() -> sessionmaker[Session]:
    return make_session_factory(make_engine(get_settings().database_url))


def get_session() -> Iterator[Session]:
    with _session_factory()() as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]


def create_app() -> FastAPI:
    app = FastAPI(title="Collection agent")

    @app.get("/healthz")
    def healthz(session: SessionDep, response: Response) -> dict[str, str]:
        try:
            session.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "db_unavailable"}
        return {"status": "ok"}

    return app


app = create_app()
