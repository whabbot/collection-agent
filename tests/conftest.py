import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.db.engine import make_engine
from collection_agent.settings import Settings, TypesafeSettings

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


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """`live` tests call real services. Deselect them unless the `-m` expression names `live`:
    an explicit `-m "not db"` replaces any default, so this cannot be left to addopts."""
    if re.search(r"(?<!not )\blive\b", config.getoption("markexpr") or ""):
        return
    live = [item for item in items if item.get_closest_marker("live")]
    if live:
        items[:] = [item for item in items if item not in live]
        config.hook.pytest_deselected(items=live)


_REAL_SERVICE_ENV = (
    "TYPESAFE_API_KEY",
    "ANTHROPIC_API_KEY",
    "COGNEE_",
    "STRIPE_",
    "POSTMARK_",
    "INBOUND_WEBHOOK_",
    "ADMIN_API_TOKEN",
    "DEMO_MODE",
    "EMAIL_SENDER",
)


@pytest.fixture(autouse=True, scope="session")
def _no_real_credentials() -> Iterator[None]:
    """Tests never read a project `.env` and never see real-service variables, so a field a
    test forgets to set can only be its default, never a real key."""
    patch = pytest.MonkeyPatch()
    for cls in (Settings, TypesafeSettings):
        patch.setitem(cls.model_config, "env_file", None)
    for name in list(os.environ):
        if name.startswith(_REAL_SERVICE_ENV):
            patch.delenv(name)
    yield
    patch.undo()
