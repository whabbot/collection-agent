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


def test_0002_round_trips_on_an_empty_db(db_engine: Engine) -> None:
    cfg = alembic_config(get_test_database_url())
    command.downgrade(cfg, "-1")
    cols = {c["name"] for c in inspect(db_engine).get_columns("invoices")}
    assert not ({"reference", "due_on"} & cols)
    assert "stripe_events" not in inspect(db_engine).get_table_names()
    command.upgrade(cfg, "head")

    insp = inspect(db_engine)
    columns = {c["name"]: c for c in insp.get_columns("invoices")}
    assert columns["reference"]["nullable"] is False
    assert columns["due_on"]["nullable"] is False
    assert columns["sent_on"]["nullable"] is True
    assert "stripe_events" in insp.get_table_names()
    event_cols = {c["name"] for c in insp.get_columns("stripe_events")}
    assert event_cols == {"id", "created_at", "event_id", "type", "stripe_object_id", "livemode"}
    uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("invoices")]
    assert ("reference",) in uniques
    event_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("stripe_events")]
    assert ("event_id",) in event_uniques
