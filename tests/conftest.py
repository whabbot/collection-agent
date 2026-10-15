import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.db.engine import make_engine

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://collection_agent:collection_agent@localhost:55432/collection_agent_test"
)
ROOT = Path(__file__).resolve().parent.parent


def get_test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)


def alembic_config(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def db_engine() -> Iterator[Engine]:
    """Test DB engine with the schema rebuilt from scratch via Alembic `upgrade head`."""
    url = get_test_database_url()
    engine = make_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    except OperationalError as exc:
        engine.dispose()
        pytest.fail(
            "Test database is unreachable. Start it with `docker compose up -d db` "
            f"(or set TEST_DATABASE_URL).\n{exc}",
            pytrace=False,
        )
    command.upgrade(alembic_config(url), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_conn(db_engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    conn = db_engine.connect()
    outer = conn.begin()
    try:
        yield conn
    finally:
        outer.rollback()
        conn.close()


@pytest.fixture
def session(db_conn: Connection) -> Iterator[Session]:
    """A session inside a transaction that is always rolled back."""
    sess = Session(bind=db_conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield sess
    finally:
        sess.close()


@pytest.fixture
def session_factory(db_conn: Connection) -> sessionmaker[Session]:
    """Sessions on the same rolled-back transaction as `session`, for code that opens and
    commits its own sessions (the daily run, the CLI)."""
    return sessionmaker(db_conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
