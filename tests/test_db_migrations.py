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


def test_0005_round_trips_and_downgrade_refuses_when_reply_rows_exist(db_engine: Engine) -> None:
    cfg = alembic_config(get_test_database_url())
    command.downgrade(cfg, "0004")
    insp = inspect(db_engine)
    old_inbound = {c["name"] for c in insp.get_columns("inbound_messages")}
    assert (
        not {"intent", "route", "review_status", "match_method", "attachment_count"} & old_inbound
    )
    old_outbound = {c["name"] for c in insp.get_columns("outbound_emails")}
    assert not {"in_reply_to_message_id", "references", "inbound_message_id"} & old_outbound
    command.upgrade(cfg, "head")

    insp = inspect(db_engine)
    inbound = {c["name"]: c for c in insp.get_columns("inbound_messages")}
    for name in (
        "postmark_message_id",
        "match_method",
        "is_auto_reply",
        "classified_text",
        "references_header",
        "intent",
        "intent_confidence",
        "intent_probabilities",
        "classifier_model",
        "promised_date",
        "promised_date_raw",
        "route",
        "review_status",
        "reviewed_by",
        "reviewed_at",
        "reviewer_intent",
        "review_note",
        "attachment_count",
    ):
        assert name in inbound
    for name in ("match_method", "is_auto_reply", "review_status", "attachment_count"):
        assert inbound[name]["nullable"] is False
        assert inbound[name]["default"] is not None  # a server default, so old rows backfill
    outbound = {c["name"] for c in insp.get_columns("outbound_emails")}
    assert {"in_reply_to_message_id", "references", "inbound_message_id"} <= outbound
    fks = insp.get_foreign_keys("outbound_emails")
    assert any(fk["referred_table"] == "inbound_messages" for fk in fks)

    # A reply-kind outbox row is a record of what a customer was sent: downgrade refuses.
    with db_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO customers (id, stripe_customer_id, name, email) "
                "VALUES ('00000000-0000-0000-0000-0000000000c1', 'cus_m5', 'A', 'a@x.test')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO invoices (id, customer_id, stripe_invoice_id, reference, "
                "amount_minor, currency, due_on, status, state, reply_token) VALUES "
                "('00000000-0000-0000-0000-0000000000a1', "
                "'00000000-0000-0000-0000-0000000000c1', 'in_m5', 'REF-M5', 100, 'GBP', "
                "'2026-11-16', 'open', 'active', 'tok-m5')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO outbound_emails (id, invoice_id, kind, to_address, from_address, "
                "from_name, reply_to, subject, body_text, attach_contract, status) VALUES "
                "('00000000-0000-0000-0000-0000000000b1', "
                "'00000000-0000-0000-0000-0000000000a1', 'reply_holding', 'a@x.test', "
                "'b@x.test', 'B', 'r@x.test', 's', 'b', false, 'pending_approval')"
            )
        )
    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(cfg, "0004")
    with db_engine.begin() as conn:
        conn.execute(text("DELETE FROM outbound_emails"))
        conn.execute(text("DELETE FROM invoices"))
        conn.execute(text("DELETE FROM customers"))
    command.downgrade(cfg, "0004")
    command.upgrade(cfg, "head")


def test_0005_check_constraints_reject_unknown_values(db_engine: Engine) -> None:
    from sqlalchemy.exc import IntegrityError

    bad = (
        "INSERT INTO inbound_messages (id, message_id, from_address, subject, body_text, "
        "received_at, counts_as_response, {col}) VALUES (gen_random_uuid(), 'm-{col}', 'a', 's', "
        "'b', now(), true, '{val}')"
    )
    for col, val in (
        ("match_method", "guess"),
        ("review_status", "maybe"),
        ("intent", "refund"),
        ("reviewer_intent", "refund"),
    ):
        with pytest.raises(IntegrityError), db_engine.begin() as conn:
            conn.execute(text(bad.format(col=col, val=val)))
