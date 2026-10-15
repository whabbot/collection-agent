import pytest
from alembic import command
from sqlalchemy import Engine, inspect

from tests.conftest import alembic_config, get_test_database_url

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "customers",
    "invoices",
    "reminders",
    "inbound_messages",
    "chase_policies",
    "alembic_version",
}


def test_upgrade_head_creates_schema(db_engine: Engine) -> None:
    assert set(inspect(db_engine).get_table_names()) >= EXPECTED_TABLES


def test_downgrade_base_then_upgrade_head_round_trips(db_engine: Engine) -> None:
    cfg = alembic_config(get_test_database_url())
    command.downgrade(cfg, "base")
    assert set(inspect(db_engine).get_table_names()) <= {"alembic_version"}
    command.upgrade(cfg, "head")
    assert set(inspect(db_engine).get_table_names()) >= EXPECTED_TABLES
