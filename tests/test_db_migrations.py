import pytest
from alembic import command
from sqlalchemy import Engine, inspect, text

from tests.conftest import alembic_config, get_test_database_url

pytestmark = pytest.mark.db

EXPECTED_TABLES = {
    "customers",
    "invoices",
    "reminders",
    "inbound_messages",
    "chase_policies",
    "contracts",
    "outbound_emails",
    "daily_runs",
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
    command.downgrade(cfg, "0001")
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


def test_0003_round_trips_and_has_the_outbox_guards(db_engine: Engine) -> None:
    cfg = alembic_config(get_test_database_url())
    command.downgrade(cfg, "0002")
    tables = set(inspect(db_engine).get_table_names())
    assert not ({"contracts", "outbound_emails"} & tables)
    with db_engine.connect() as conn:
        left = conn.execute(text("SELECT 1 FROM pg_type WHERE typname = 'outbound_status'")).all()
    assert left == []
    command.upgrade(cfg, "head")

    insp = inspect(db_engine)
    contract_uniques = [tuple(u["column_names"]) for u in insp.get_unique_constraints("contracts")]
    assert ("invoice_id",) in contract_uniques
    indexes = {i["name"]: i for i in insp.get_indexes("outbound_emails")}
    assert indexes["ix_outbound_emails_status"]["column_names"] == ["status"]
    guard = indexes["uq_outbound_emails_one_live_invoice_email"]
    assert guard["unique"] is True
    assert guard["column_names"] == ["invoice_id"]
    where = guard["dialect_options"]["postgresql_where"]
    assert "invoice" in where
    assert "rejected" in where
    assert "failed" in where


def test_0004_round_trips_and_has_the_reminder_guard(db_engine: Engine) -> None:
    cfg = alembic_config(get_test_database_url())
    command.downgrade(cfg, "0003")
    insp = inspect(db_engine)
    assert "daily_runs" not in insp.get_table_names()
    assert "escalated_at" not in {c["name"] for c in insp.get_columns("invoices")}
    old_cols = {c["name"] for c in insp.get_columns("outbound_emails")}
    assert not ({"resolved_by", "resolved_at"} & old_cols)
    assert "uq_outbound_emails_one_active_reminder" not in {
        i["name"] for i in insp.get_indexes("outbound_emails")
    }
    command.upgrade(cfg, "head")

    insp = inspect(db_engine)
    assert "daily_runs" in insp.get_table_names()
    assert {c["name"] for c in insp.get_columns("daily_runs")} == {
        "id",
        "run_date",
        "started_at",
        "finished_at",
        "dry_run",
        "counts",
    }
    assert "escalated_at" in {c["name"] for c in insp.get_columns("invoices")}
    assert {"resolved_by", "resolved_at"} <= {
        c["name"] for c in insp.get_columns("outbound_emails")
    }
    indexes = {i["name"]: i for i in insp.get_indexes("outbound_emails")}
    guard = indexes["uq_outbound_emails_one_active_reminder"]
    assert guard["unique"] is True
    assert guard["column_names"] == ["invoice_id"]
    where = guard["dialect_options"]["postgresql_where"]
    assert "pending_approval" in where
    assert "sending" in where
    assert "reminder_broken_promise" in where
