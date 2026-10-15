"""Demo mode without a database: gating, settings, the log sender guard, static files, reset
guards. A poison session proves none of it touches the DB."""

import re
from datetime import datetime

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from collection_agent.cli import main
from collection_agent.deps import LONDON, get_email_sender, get_now, get_today
from collection_agent.settings import Settings
from tests.api_helpers import AUTH, PoisonSession, make_client, make_settings

DEMO = make_settings(demo_mode=True, email_sender="log")

DEMO_GETS = [
    "/demo",
    "/demo/demo.js",
    "/demo/demo.css",
    "/demo/api/clock",
    "/demo/api/sent-emails",
    "/demo/api/sample-policy",
]
DEMO_POSTS = [
    ("/demo/api/clock", {"today": "2026-10-01"}),
    ("/demo/api/clock/advance", {"days": 7}),
    ("/demo/api/run-daily", None),
    (
        "/demo/api/simulate-reply",
        {"invoice_id": "6f0f6d0e-0000-4000-8000-000000000000", "text": "x"},
    ),
]


# --- off by default: every demo route is a 404 -------------------------------------------------


@pytest.mark.parametrize("path", DEMO_GETS)
def test_demo_gets_404_when_demo_mode_is_off(path: str) -> None:
    client = make_client()  # demo_mode defaults to False
    assert client.get(path).status_code == 404
    assert client.get(path, headers=AUTH).status_code == 404


@pytest.mark.parametrize(("path", "body"), DEMO_POSTS)
def test_demo_posts_404_when_demo_mode_is_off(path: str, body: dict[str, object] | None) -> None:
    client = make_client()
    assert client.post(path, json=body, headers=AUTH).status_code == 404


def test_demo_mode_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.delenv("EMAIL_SENDER", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.demo_mode is False
    assert s.email_sender == "postmark"


# --- demo mode: admin auth still applies to the api, not to the page ---------------------------


@pytest.mark.parametrize("path", [p for p in DEMO_GETS if p.startswith("/demo/api")])
def test_demo_api_needs_the_admin_token(path: str) -> None:
    client = make_client(settings=DEMO, session=PoisonSession())  # type: ignore[arg-type]
    assert client.get(path).status_code == 401


def test_page_and_assets_are_served_without_a_token() -> None:
    client = make_client(settings=DEMO)
    page = client.get("/demo")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "/demo/demo.js" in page.text and "/demo/demo.css" in page.text
    js = client.get("/demo/demo.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    css = client.get("/demo/demo.css")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")


def test_page_has_no_inline_secrets_and_no_cdn() -> None:
    client = make_client(settings=DEMO)
    for path in ("/demo", "/demo/demo.js", "/demo/demo.css"):
        text = client.get(path).text
        assert not re.search(
            r"sk_(test|live)_|whsec_|pm_test|admin_test_fake|Bearer [A-Za-z0-9]", text
        )
        assert not re.search(r"https?://(?!www\.w3\.org)", text), path  # nothing loaded remotely


# --- the log email sender is demo-only ---------------------------------------------------------


def test_settings_refuse_the_log_sender_outside_demo_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("EMAIL_SENDER", "log")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with pytest.raises(ValidationError, match="EMAIL_SENDER=log"):
        Settings(_env_file=None)  # type: ignore[call-arg]
    monkeypatch.setenv("DEMO_MODE", "true")
    assert Settings(_env_file=None).email_sender == "log"  # type: ignore[call-arg]


def test_unknown_email_sender_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:x@127.0.0.1:1/x")
    monkeypatch.setenv("EMAIL_SENDER", "smtp")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_the_dependency_also_refuses_the_log_sender_outside_demo_mode() -> None:
    settings = make_settings(email_sender="log", demo_mode=False)
    with pytest.raises(HTTPException) as exc:
        get_email_sender(settings, PoisonSession())  # type: ignore[arg-type]
    assert exc.value.status_code == 503
    assert exc.value.detail == {"error": "log_email_sender_requires_demo_mode"}


def test_approve_with_the_log_sender_outside_demo_mode_sends_nothing() -> None:
    client = make_client(settings=make_settings(email_sender="log", demo_mode=False))
    resp = client.post(
        "/outbox/6f0f6d0e-0000-4000-8000-000000000000/approve",
        json={"approved_by": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 503
    assert resp.json() == {"detail": {"error": "log_email_sender_requires_demo_mode"}}


# --- the clock dependencies change nothing when demo mode is off -------------------------------


def test_get_today_and_get_now_are_the_real_clock_outside_demo_mode() -> None:
    settings = make_settings(demo_mode=False)
    session = PoisonSession()  # the clock table must not even be read
    assert get_today(settings, session) == datetime.now(LONDON).date()  # type: ignore[arg-type]
    assert abs((get_now(settings, session) - datetime.now(LONDON)).total_seconds()) < 5  # type: ignore[arg-type]


# --- reset CLI guards (all before any database access) -----------------------------------------


def test_reset_refuses_without_demo_mode(capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(
        demo_mode=False, database_url="postgresql+psycopg://x:x@127.0.0.1:1/collection_agent"
    )
    code = main(["demo", "reset", "--yes"], settings=settings)
    assert code == 1
    assert "DEMO_MODE" in capsys.readouterr().err


def test_reset_refuses_a_database_that_is_not_collection_agent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(
        demo_mode=True, database_url="postgresql+psycopg://x:x@127.0.0.1:1/production"
    )
    code = main(["demo", "reset", "--yes"], settings=settings)
    err = capsys.readouterr().err
    assert code == 1
    assert "collection_agent" in err
    assert "x:x" not in err and "production" not in err  # the URL is never echoed
