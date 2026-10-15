"""Demo helpers against the test database: clock, run-daily, simulate-reply, log sender, reset."""

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.classifier import Intent
from collection_agent.cli import main
from collection_agent.db.models import (
    ChasePolicyRow,
    DemoClock,
    DemoSentEmail,
    InboundMessage,
    Invoice,
    OutboundEmail,
    OutboundStatus,
)
from collection_agent.deps import LONDON, get_now, get_today
from tests.api_helpers import AUTH, make_client, make_settings
from tests.daily_helpers import approve_default_policy, chased_invoice
from tests.db_helpers import make_invoice
from tests.fake_classifier import FakeClassifier, make_classification, relative
from tests.fake_stripe import FakeStripeGateway
from tests.inbound_helpers import sent_outbox

pytestmark = pytest.mark.db

DEMO = make_settings(demo_mode=True, email_sender="log")
DEMO_POSTMARK = make_settings(demo_mode=True)


def demo_client(session: Session, **kw: Any) -> Any:
    return make_client(session=session, settings=kw.pop("settings", DEMO), real_clock=True, **kw)


# --- clock ---------------------------------------------------------------------------------------


def test_clock_defaults_to_the_real_london_date_until_set(session: Session) -> None:
    body = demo_client(session).get("/demo/api/clock", headers=AUTH).json()
    assert body["today"] == datetime.now(LONDON).date().isoformat()
    assert body["is_set"] is False


def test_set_then_advance_the_clock(session: Session) -> None:
    client = demo_client(session)
    resp = client.post("/demo/api/clock", json={"today": "2026-10-01"}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["today"] == "2026-10-01" and resp.json()["is_set"] is True

    advanced = client.post("/demo/api/clock/advance", json={"days": 7}, headers=AUTH)
    assert advanced.json()["today"] == "2026-10-08"
    assert client.get("/demo/api/clock", headers=AUTH).json()["today"] == "2026-10-08"
    assert session.scalar(select(func.count()).select_from(DemoClock)) == 1  # a single row


def test_advance_with_no_stored_date_starts_from_the_real_date(session: Session) -> None:
    resp = demo_client(session).post("/demo/api/clock/advance", json={"days": 2}, headers=AUTH)
    assert resp.status_code == 200
    real = datetime.now(LONDON).date()
    assert (date.fromisoformat(resp.json()["today"]) - real).days in (2, 1, 3)  # midnight race


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/demo/api/clock", {"today": "not-a-date"}),
        ("/demo/api/clock", {}),
        ("/demo/api/clock/advance", {"days": 0}),
        ("/demo/api/clock/advance", {"days": -3}),
        ("/demo/api/clock/advance", {"days": 100000}),
        ("/demo/api/clock/advance", {"days": 7, "extra": 1}),
    ],
)
def test_clock_rejects_bad_input(session: Session, path: str, body: dict[str, Any]) -> None:
    assert demo_client(session).post(path, json=body, headers=AUTH).status_code == 422


def test_dependencies_use_the_stored_clock_in_demo_mode(session: Session) -> None:
    session.add(DemoClock(id=1, today=date(2026, 10, 8)))
    session.flush()
    assert get_today(DEMO, session) == date(2026, 10, 8)
    now = get_now(DEMO, session)
    assert now.tzinfo is not None
    assert now.astimezone(LONDON).date() == date(2026, 10, 8)


def test_dependencies_ignore_the_stored_clock_outside_demo_mode(session: Session) -> None:
    session.add(DemoClock(id=1, today=date(2026, 10, 8)))
    session.flush()
    off = make_settings(demo_mode=False)
    assert get_today(off, session) == datetime.now(LONDON).date()
    assert abs((get_now(off, session) - datetime.now(UTC)).total_seconds()) < 5


def test_endpoints_see_the_demo_clock(session: Session) -> None:
    """`sent_on` is stamped from the injected `today`: with the demo clock it is the stored date."""
    approve_default_policy(session)
    inv = chased_invoice(session, sent_on=None)
    row = sent_outbox(session, inv, "x" * 12)
    row.kind, row.status = "invoice", OutboundStatus.PENDING_APPROVAL
    session.add(DemoClock(id=1, today=date(2026, 10, 1)))
    session.flush()

    resp = demo_client(session).post(
        f"/outbox/{row.id}/approve", json={"approved_by": "t"}, headers=AUTH
    )

    assert resp.status_code == 200
    session.refresh(inv)
    assert inv.sent_on == date(2026, 10, 1)


def test_cli_default_date_uses_the_clock_in_demo_mode(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    approve_default_policy(session)
    session.add(DemoClock(id=1, today=date(2026, 10, 8)))
    session.flush()

    code = main(
        ["run-daily", "--dry-run"],
        settings=make_settings(demo_mode=True),
        session_factory=session_factory,
    )

    assert code == 0
    assert json.loads(capsys.readouterr().out)["run_date"] == "2026-10-08"


def test_cli_default_date_ignores_the_clock_outside_demo_mode(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    approve_default_policy(session)
    session.add(DemoClock(id=1, today=date(2026, 10, 8)))
    session.flush()

    main(["run-daily", "--dry-run"], settings=make_settings(), session_factory=session_factory)

    assert (
        json.loads(capsys.readouterr().out)["run_date"] == datetime.now(LONDON).date().isoformat()
    )


# --- run-daily -----------------------------------------------------------------------------------


def test_run_daily_uses_the_clock_date_and_drafts_a_tagged_reminder(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)  # sent 1 Oct, due 16 Nov
    session.add(DemoClock(id=1, today=date(2026, 10, 8)))  # a Thursday, 7 days later
    session.flush()
    gateway = FakeStripeGateway()
    client = demo_client(session, gateway=gateway, session_factory=session_factory)

    resp = client.post("/demo/api/run-daily", headers=AUTH)

    assert resp.status_code == 200
    report = resp.json()
    assert report["run_date"] == "2026-10-08"
    assert report["counts"] == {"drafted:reminder[no_response]": 1}
    row = session.scalars(select(OutboundEmail).where(OutboundEmail.invoice_id == inv.id)).one()
    assert row.rule_code == "no_response"
    assert row.rule_label is not None and "No response for 7 days" in row.rule_label


def test_run_daily_without_an_approved_policy_is_409(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    client = demo_client(session, gateway=FakeStripeGateway(), session_factory=session_factory)
    resp = client.post("/demo/api/run-daily", headers=AUTH)
    assert resp.status_code == 409
    assert resp.json() == {"error": "no_approved_policy"}


def test_run_daily_fails_closed_without_stripe_or_identity(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    no_identity = make_settings(demo_mode=True, company_name=None)
    resp = demo_client(
        session, settings=no_identity, gateway=FakeStripeGateway(), session_factory=session_factory
    ).post("/demo/api/run-daily", headers=AUTH)
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"error": "email_not_configured"}}


# --- simulate-reply ------------------------------------------------------------------------------


def promise_classifier() -> FakeClassifier:
    return FakeClassifier(
        make_classification(Intent.PROMISE_TO_PAY, 0.93, date_parts=relative("tomorrow"))
    )


def test_simulate_reply_runs_the_real_inbound_flow(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    sent_outbox(session, inv, "pm-provider-id-0001")
    session.add(DemoClock(id=1, today=date(2026, 10, 9)))
    session.flush()
    classifier = promise_classifier()
    client = demo_client(session, classifier=classifier, gateway=FakeStripeGateway())

    resp = client.post(
        "/demo/api/simulate-reply",
        json={"invoice_id": str(inv.id), "text": "We'll pay on the 12th."},
        headers=AUTH,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["route"] == "promise_accepted"
    assert classifier.calls and classifier.calls[0][0] == "We'll pay on the 12th."
    message = session.get(InboundMessage, body["inbound_message_id"])
    assert message is not None
    assert message.invoice_id == inv.id
    assert message.match_method == "reply_token"
    assert message.subject == "Re: Reminder"
    assert message.from_address == "ap@acme.test"
    assert message.intent == "promise_to_pay"
    session.refresh(inv)
    assert inv.promised_date == date(2026, 10, 10)
    assert inv.paused_until is not None
    ack = session.scalars(
        select(OutboundEmail).where(OutboundEmail.inbound_message_id == message.id)
    ).one()
    assert ack.kind == "reply_promise_ack"
    assert ack.status is OutboundStatus.PENDING_APPROVAL
    assert ack.in_reply_to_message_id is not None  # threads onto the simulated message


def test_simulate_reply_payload_threads_to_the_last_sent_email(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    sent_outbox(session, inv, "pm-provider-id-0001")
    classifier = promise_classifier()
    client = demo_client(session, classifier=classifier, gateway=FakeStripeGateway())

    captured: list[dict[str, Any]] = []
    from collection_agent import demo as demo_module

    original = demo_module.process_inbound

    def spy(session: Session, payload: Any, *a: Any, **k: Any) -> Any:
        captured.append(payload)
        return original(session, payload, *a, **k)

    demo_module.process_inbound = spy
    try:
        client.post(
            "/demo/api/simulate-reply",
            json={"invoice_id": str(inv.id), "text": "Soon.", "from_email": "Other@Acme.test"},
            headers=AUTH,
        )
    finally:
        demo_module.process_inbound = original

    (payload,) = captured
    assert payload["MailboxHash"] == inv.reply_token
    assert payload["Subject"] == "Re: Reminder"
    assert payload["TextBody"] == "Soon." and payload["StrippedTextReply"] == "Soon."
    assert payload["FromFull"]["Email"] == "Other@Acme.test"
    headers = {h["Name"]: h["Value"] for h in payload["Headers"]}
    assert "pm-provider-id-0001" in headers["In-Reply-To"]
    assert headers["Message-ID"].startswith("<demo-")


def test_each_simulated_reply_has_a_fresh_message_id(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    client = demo_client(session, classifier=promise_classifier(), gateway=FakeStripeGateway())
    body = {"invoice_id": str(inv.id), "text": "Hello"}
    first = client.post("/demo/api/simulate-reply", json=body, headers=AUTH).json()
    second = client.post("/demo/api/simulate-reply", json=body, headers=AUTH).json()
    assert first["inbound_message_id"] != second["inbound_message_id"]


def test_simulate_reply_without_a_classifier_shows_the_real_failure(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    client = demo_client(session, classifier=None, gateway=FakeStripeGateway())
    resp = client.post(
        "/demo/api/simulate-reply",
        json={"invoice_id": str(inv.id), "text": "We will pay soon"},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["route"] == "classification_failed"
    assert resp.json()["review_status"] == "needs_review"


def test_simulate_reply_for_an_unknown_invoice_is_404(session: Session) -> None:
    resp = demo_client(session).post(
        "/demo/api/simulate-reply",
        json={"invoice_id": "6f0f6d0e-0000-4000-8000-000000000000", "text": "hi"},
        headers=AUTH,
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": "invoice_not_found"}


def test_simulate_reply_rejects_empty_text(session: Session) -> None:
    inv = make_invoice(session)
    resp = demo_client(session).post(
        "/demo/api/simulate-reply", json={"invoice_id": str(inv.id), "text": "  "}, headers=AUTH
    )
    assert resp.status_code == 422


# --- the log email sender ------------------------------------------------------------------------


def test_approve_with_the_log_sender_records_a_demo_email(session: Session) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session, sent_on=None)
    row = sent_outbox(session, inv, "x" * 12)
    row.kind, row.status = "invoice", OutboundStatus.PENDING_APPROVAL
    row.provider_message_id = None
    row.attach_contract = False
    session.flush()
    client = demo_client(session)

    resp = client.post(f"/outbox/{row.id}/approve", json={"approved_by": "t"}, headers=AUTH)

    assert resp.status_code == 200
    assert resp.json()["status"] == "sent"
    assert resp.json()["provider_message_id"].startswith("log-")
    stored = session.scalars(select(DemoSentEmail)).one()
    assert stored.to_address == "ap@acme.test"
    assert stored.subject == "Reminder"
    assert stored.provider_message_id == resp.json()["provider_message_id"]
    listed = client.get("/demo/api/sent-emails", headers=AUTH).json()
    assert [e["subject"] for e in listed] == ["Reminder"]
    assert listed[0]["attachment_names"] == []


def test_postmark_sender_setting_still_requires_the_token_in_demo_mode(session: Session) -> None:
    client = demo_client(
        session, settings=make_settings(demo_mode=True, postmark_server_token=None)
    )
    resp = client.post(
        "/outbox/6f0f6d0e-0000-4000-8000-000000000000/approve",
        json={"approved_by": "t"},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"error": "postmark_not_configured"}}


# --- sample policy -------------------------------------------------------------------------


def test_sample_policy_is_the_fixture(session: Session) -> None:
    resp = demo_client(session).get("/demo/api/sample-policy", headers=AUTH)
    assert resp.status_code == 200
    assert "Credit Control Policy" in resp.json()["text"]


# --- reset CLI -----------------------------------------------------------------------------


def reset_settings() -> Any:
    from tests.conftest import get_test_database_url

    return make_settings(demo_mode=True, database_url=get_test_database_url())


def test_reset_without_yes_needs_the_typed_confirmation(
    session: Session, session_factory: sessionmaker[Session], capsys: pytest.CaptureFixture[str]
) -> None:
    approve_default_policy(session)
    make_invoice(session)
    session.flush()

    code = main(
        ["demo", "reset"],
        settings=reset_settings(),
        session_factory=session_factory,
        confirm=lambda _prompt: "nope",
    )

    assert code == 1
    assert session.scalar(select(func.count()).select_from(Invoice)) == 1
    assert session.scalar(select(func.count()).select_from(ChasePolicyRow)) == 1


def test_reset_with_typed_confirmation_deletes_demo_data(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    sent_outbox(session, inv, "pm-provider-id-0001")
    session.add(
        DemoSentEmail(to_address="a@b.test", subject="s", body_text="b", attachment_names=[])
    )
    session.flush()

    code = main(
        ["demo", "reset"],
        settings=reset_settings(),
        session_factory=session_factory,
        confirm=lambda _prompt: "reset",
    )

    assert code == 0
    for model in (Invoice, OutboundEmail, ChasePolicyRow, DemoSentEmail):
        assert session.scalar(select(func.count()).select_from(model)) == 0


def test_reset_yes_skips_the_prompt(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    make_invoice(session)
    session.flush()

    def no_prompt(_prompt: str) -> str:
        raise AssertionError("must not prompt with --yes")

    code = main(
        ["demo", "reset", "--yes"],
        settings=reset_settings(),
        session_factory=session_factory,
        confirm=no_prompt,
    )

    assert code == 0
    assert session.scalar(select(func.count()).select_from(Invoice)) == 0


def test_reset_leaves_the_clock_alone(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    session.add(DemoClock(id=1, today=date(2026, 10, 1)))
    session.flush()
    main(["demo", "reset", "--yes"], settings=reset_settings(), session_factory=session_factory)
    assert session.scalar(select(func.count()).select_from(DemoClock)) == 1
