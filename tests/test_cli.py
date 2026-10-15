import json

import pytest
import stripe
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.cli import main
from collection_agent.db.models import DailyRun, OutboundEmail
from collection_agent.settings import Settings
from tests.api_helpers import make_settings
from tests.daily_helpers import approve_default_policy, chased_invoice
from tests.fake_stripe import FakeStripeGateway

DATE = ["--date", "2026-10-08"]


def cli(
    argv: list[str],
    *,
    settings: Settings | None = None,
    factory: sessionmaker[Session] | None = None,
    gateway: FakeStripeGateway | None = None,
) -> int:
    return main(
        argv, settings=settings or make_settings(), session_factory=factory, gateway=gateway
    )


# --- no database -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unset", "name"),
    [
        ("stripe_secret_key", "STRIPE_SECRET_KEY"),
        ("email_from_address", "EMAIL_FROM_ADDRESS"),
        ("email_from_name", "EMAIL_FROM_NAME"),
        ("reply_to_domain", "REPLY_TO_DOMAIN"),
        ("company_name", "COMPANY_NAME"),
    ],
)
def test_live_run_fails_closed_naming_the_variable_but_no_value(
    capsys: pytest.CaptureFixture[str], unset: str, name: str
) -> None:
    settings = make_settings(**{unset: None})

    code = cli(["run-daily", *DATE], settings=settings)

    captured = capsys.readouterr()
    assert code == 1
    assert name in captured.err
    assert captured.out == ""
    for secret in ("sk_test_fake_not_real", "pm_test_fake_not_real", "admin_test_fake"):
        assert secret not in captured.err + captured.out


def test_missing_database_url_exits_1(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pytest.TempPathFactory,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert main(["run-daily", "--dry-run"]) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_bad_date_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exc:
        cli(["run-daily", "--date", "08/10/2026"])
    assert exc.value.code == 2


# --- with the database ------------------------------------------------------------------------


@pytest.mark.db
def test_policy_create_approve_show_round_trip(
    capsys: pytest.CaptureFixture[str], session_factory: sessionmaker[Session]
) -> None:
    assert cli(["policy", "show"], factory=session_factory) == 1
    assert "no approved" in capsys.readouterr().err.lower()

    assert cli(["policy", "create-default"], factory=session_factory) == 0
    created = capsys.readouterr().out
    version = int(created.split()[-1])

    assert (
        cli(
            ["policy", "approve", "--version", str(version), "--by", "sam"], factory=session_factory
        )
        == 0
    )
    capsys.readouterr()

    assert cli(["policy", "show"], factory=session_factory) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "no_response_days": 7,
        "clearance_day_of_month": 15,
        "clearance_month_offset": 1,
        "pre_deadline_buffer_working_days": 3,
        "max_reminders": 3,
        "promise_grace_working_days": 2,
    }


@pytest.mark.db
def test_policy_approve_unknown_or_repeated_version_exits_1(
    capsys: pytest.CaptureFixture[str], session_factory: sessionmaker[Session]
) -> None:
    assert (
        cli(["policy", "approve", "--version", "9999", "--by", "sam"], factory=session_factory) == 1
    )
    assert "9999" in capsys.readouterr().err


@pytest.mark.db
def test_dry_run_prints_json_and_exits_0(
    capsys: pytest.CaptureFixture[str], session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session, reference="INV-1")
    gateway = FakeStripeGateway()
    # No Stripe key and no email identity: a dry run needs only the database.
    settings = make_settings(
        stripe_secret_key=None, company_name=None, email_from_name=None, reply_to_domain=None
    )

    code = cli(
        ["run-daily", "--dry-run", *DATE],
        settings=settings,
        factory=session_factory,
        gateway=gateway,
    )

    assert code == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] is True
    assert report["run_date"] == "2026-10-08"
    assert report["counts"] == {"would_drafted:reminder": 1}
    assert report["invoices"][0]["reference"] == "INV-1"
    assert gateway.calls == []
    assert session.scalars(select(OutboundEmail)).all() == []
    assert session.scalar(select(func.count(DailyRun.id))) == 0


@pytest.mark.db
def test_no_approved_policy_exits_1(
    capsys: pytest.CaptureFixture[str], session_factory: sessionmaker[Session]
) -> None:
    assert cli(["run-daily", "--dry-run", *DATE], factory=session_factory) == 1
    captured = capsys.readouterr()
    assert "no approved ChasePolicy" in captured.err
    assert captured.out == ""


@pytest.mark.db
def test_live_run_drafts_and_records_the_run(
    capsys: pytest.CaptureFixture[str], session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    chased_invoice(session)

    code = cli(["run-daily", *DATE], factory=session_factory, gateway=FakeStripeGateway())

    assert code == 0
    assert json.loads(capsys.readouterr().out)["counts"] == {"drafted:reminder": 1}
    assert len(session.scalars(select(OutboundEmail)).all()) == 1
    assert session.scalar(select(func.count(DailyRun.id))) == 1


@pytest.mark.db
def test_stripe_error_exits_2_but_still_prints_the_report(
    capsys: pytest.CaptureFixture[str], session: Session, session_factory: sessionmaker[Session]
) -> None:
    approve_default_policy(session)
    inv = chased_invoice(session)
    gateway = FakeStripeGateway()
    gateway.status_errors[inv.stripe_invoice_id] = stripe.APIConnectionError("down")

    code = cli(["run-daily", *DATE], factory=session_factory, gateway=gateway)

    assert code == 2
    assert json.loads(capsys.readouterr().out)["counts"] == {"stripe_error": 1}
