"""Command line: the daily run and ChasePolicy management.

Exit codes for `run-daily`: 0 success; 1 no approved policy or missing required settings;
2 an invoice ended in `stripe_error` or an unexpected error (or the run itself failed).
Messages name environment variables, never their values.
"""

import argparse
import json
import logging
import sys
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.classifier import Intent, JevClassifier, ReplyClassifier
from collection_agent.classifier_claude import ClaudeClassifier
from collection_agent.classifier_eval import DEFAULT_FIXTURES, load_cases, run_eval
from collection_agent.cognee_client import CogneeGateway, LiveCognee
from collection_agent.daily import run_daily
from collection_agent.db.engine import make_engine, make_session_factory
from collection_agent.db.repository import (
    NoApprovedPolicyError,
    PolicyStateError,
    approve_policy,
    create_draft_policy,
    get_approved_policy,
    get_policy_row,
)
from collection_agent.demo_reset import DemoResetRefused, confirm_and_reset
from collection_agent.demo_support import clock_today
from collection_agent.deps import (
    LONDON,
    EmailIdentity,
    identity_from_settings,
    missing_identity_settings,
)
from collection_agent.policy import ChasePolicy
from collection_agent.policy_api import MAX_POLICY_CHARS, MIN_POLICY_CHARS
from collection_agent.policy_cards import render_cards
from collection_agent.policy_extraction import ClaudeExtractor, ExtractionError, PolicyExtractor
from collection_agent.policy_ingest import cards_for, extract_and_draft, store_and_ingest
from collection_agent.settings import Settings, TypesafeSettings
from collection_agent.stripe_gateway import LiveStripeGateway, StripeGateway

OK = 0
USAGE_OR_CONFIG = 1
RUN_PROBLEM = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="collection-agent")
    sub = parser.add_subparsers(dest="command", required=True)

    daily = sub.add_parser("run-daily", help="decide and draft today's reminders")
    daily.add_argument(
        "--date", type=date.fromisoformat, help="YYYY-MM-DD (default: today, London)"
    )
    daily.add_argument("--dry-run", action="store_true", help="decide and render; write nothing")

    policy = sub.add_parser("policy", help="manage the ChasePolicy")
    psub = policy.add_subparsers(dest="policy_command", required=True)
    psub.add_parser("create-default", help="insert a draft with the decided defaults")
    psub.add_parser("show", help="print the approved policy")
    ingest = psub.add_parser(
        "ingest", help="store policy text, send it to Cognee, and have Claude propose a draft"
    )
    ingest.add_argument("--title", required=True)
    ingest.add_argument("--file", type=Path, required=True, help="a UTF-8 text file")
    approve = psub.add_parser("approve", help="approve a draft policy version")
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--by", required=True, help="who is approving")

    demo = sub.add_parser("demo", help="demo-mode tools (need DEMO_MODE=true)")
    dsub = demo.add_subparsers(dest="demo_command", required=True)
    reset = dsub.add_parser(
        "reset", help="delete the demo invoices, outbox, inbound mail, Stripe events and policies"
    )
    reset.add_argument("--yes", action="store_true", help="do not ask for typed confirmation")

    ev = sub.add_parser(
        "eval-classifier",
        help="run a reply classifier over the synthetic replies (jev needs TYPESAFE_API_KEY, "
        "claude needs ANTHROPIC_API_KEY)",
    )
    ev.add_argument(
        "--classifier",
        choices=["jev", "claude"],
        default=None,
        help="which classifier to run (default: CLASSIFIER from the environment, else jev)",
    )
    ev.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES, help="a .jsonl file")
    return parser


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    gateway: StripeGateway | None = None,
    classifiers: tuple[ReplyClassifier, ReplyClassifier] | None = None,
    cognee: CogneeGateway | None = None,
    extractor: PolicyExtractor | None = None,
    confirm: Callable[[str], str] = input,
) -> int:
    """Entry point. The keyword arguments exist so tests can inject fakes; the console script
    passes none and everything comes from the environment."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    args = _build_parser().parse_args(argv)

    if args.command == "eval-classifier":
        return _eval_classifier(args, classifiers)

    if settings is None:
        try:
            settings = Settings()
        except ValidationError:
            _err("DATABASE_URL is not set")
            return USAGE_OR_CONFIG
    factory = session_factory or make_session_factory(make_engine(settings.database_url))

    if args.command == "policy":
        return _policy(args, factory, settings, cognee, extractor)
    if args.command == "demo":
        return _demo_reset(args, settings, factory, confirm)
    return _run_daily(args, settings, factory, gateway)


def _eval_classifier(
    args: argparse.Namespace, classifiers: tuple[ReplyClassifier, ReplyClassifier] | None
) -> int:
    """Needs no database. Real classifiers are built only here, from the environment."""
    if classifiers is None:
        ts = TypesafeSettings()
        kind = args.classifier or ts.classifier
        reverse = list(reversed(Intent))
        if kind == "claude":
            if not ts.anthropic_api_key:
                _err("missing required settings: ANTHROPIC_API_KEY")
                return USAGE_OR_CONFIG
            classifiers = (
                ClaudeClassifier(ts.anthropic_api_key, ts.anthropic_model),
                ClaudeClassifier(ts.anthropic_api_key, ts.anthropic_model, intent_order=reverse),
            )
        else:
            if not ts.typesafe_api_key:
                _err("missing required settings: TYPESAFE_API_KEY")
                return USAGE_OR_CONFIG
            classifiers = (
                JevClassifier(ts.typesafe_api_key, ts.jev_model),
                JevClassifier(ts.typesafe_api_key, ts.jev_model, intent_order=reverse),
            )
    try:
        cases = load_cases(args.fixtures)
    except (OSError, ValueError, KeyError) as exc:
        _err(f"could not read fixtures: {type(exc).__name__}")
        return USAGE_OR_CONFIG
    report = run_eval(classifiers[0], cases, classifiers[1])
    if report.fatal is not None:
        _err(report.fatal)
        return USAGE_OR_CONFIG
    print(report.render())
    return OK


def _policy(
    args: argparse.Namespace,
    factory: sessionmaker[Session],
    settings: Settings,
    cognee: CogneeGateway | None,
    extractor: PolicyExtractor | None,
) -> int:
    if args.policy_command == "ingest":
        return _policy_ingest(args, factory, settings, cognee, extractor)
    with factory() as session:
        match args.policy_command:
            case "create-default":
                version = create_draft_policy(session, ChasePolicy())
                session.commit()
                print(f"created draft policy version {version}")
            case "show":
                try:
                    policy = get_approved_policy(session)
                except NoApprovedPolicyError:
                    _err("no approved ChasePolicy")
                    return USAGE_OR_CONFIG
                print(json.dumps(policy.model_dump(), indent=2))
            case "approve":
                try:
                    approve_policy(session, args.version, args.by)
                except PolicyStateError as exc:
                    session.rollback()
                    _err(str(exc))
                    return USAGE_OR_CONFIG
                session.commit()
                print(f"approved policy version {args.version}")
    return OK


def _policy_ingest(
    args: argparse.Namespace,
    factory: sessionmaker[Session],
    settings: Settings,
    cognee: CogneeGateway | None,
    extractor: PolicyExtractor | None,
) -> int:
    try:
        text = args.file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _err(f"could not read the policy file: {type(exc).__name__}")
        return USAGE_OR_CONFIG
    if not MIN_POLICY_CHARS <= len(text) <= MAX_POLICY_CHARS:
        _err(f"the policy text must be {MIN_POLICY_CHARS} to {MAX_POLICY_CHARS} characters")
        return USAGE_OR_CONFIG
    if cognee is None and settings.cognee_base_url and settings.cognee_api_key:
        cognee = LiveCognee(settings.cognee_base_url, settings.cognee_api_key)
    if extractor is None and settings.anthropic_api_key:
        extractor = ClaudeExtractor(settings.anthropic_api_key, settings.anthropic_model)

    with factory() as session:
        doc = store_and_ingest(session, args.title, text, cognee, settings.cognee_policies_dataset)
        session.commit()  # the text stays stored whatever happens next
        print(f"stored document {doc.id} (cognee: {doc.cognee_status})")
        if extractor is None:
            _err("missing required settings: ANTHROPIC_API_KEY")
            return USAGE_OR_CONFIG
        try:
            version = extract_and_draft(session, doc, extractor)
        except ExtractionError as exc:
            session.rollback()
            _err(f"extraction failed: {exc}")
            return RUN_PROBLEM
        row = get_policy_row(session, version)
        assert row is not None
        card_set = cards_for(session, row)
        session.commit()
    print(render_cards(card_set.version, card_set.status, card_set.cards, card_set.soft_guidance))
    print(f"\nreview it, then: collection-agent policy approve --version {version} --by <name>")
    return OK


def _default_run_date(settings: Settings, factory: sessionmaker[Session]) -> date:
    """Today in London; in demo mode the simulated clock's date."""
    if settings.demo_mode:
        with factory() as session:
            return clock_today(session)
    return datetime.now(LONDON).date()


def _demo_reset(
    args: argparse.Namespace,
    settings: Settings,
    factory: sessionmaker[Session],
    confirm: Callable[[str], str],
) -> int:
    try:
        deleted = confirm_and_reset(settings, factory, assume_yes=args.yes, confirm=confirm)
    except DemoResetRefused as exc:
        _err(str(exc))
        return USAGE_OR_CONFIG
    if deleted is None:
        _err("not confirmed; nothing deleted")
        return USAGE_OR_CONFIG
    print(json.dumps(deleted, indent=2))
    return OK


def _run_daily(
    args: argparse.Namespace,
    settings: Settings,
    factory: sessionmaker[Session],
    gateway: StripeGateway | None,
) -> int:
    identity: EmailIdentity | None = identity_from_settings(settings)
    if not args.dry_run:
        missing = missing_identity_settings(settings)
        if gateway is None and not settings.stripe_secret_key:
            missing.insert(0, "STRIPE_SECRET_KEY")
        if missing:
            _err(f"missing required settings: {', '.join(missing)}")
            return USAGE_OR_CONFIG
        if gateway is None:
            assert settings.stripe_secret_key is not None
            gateway = LiveStripeGateway(settings.stripe_secret_key)
    else:
        gateway = None  # a dry run never calls Stripe

    run_date = args.date or _default_run_date(settings, factory)
    try:
        report = run_daily(factory, gateway, identity, run_date, dry_run=args.dry_run)
    except NoApprovedPolicyError:
        _err("no approved ChasePolicy: run `collection-agent policy create-default` and approve it")
        return USAGE_OR_CONFIG
    except Exception as exc:
        _err(f"daily run failed: {type(exc).__name__}")
        return RUN_PROBLEM

    print(json.dumps(report.to_dict(), indent=2))
    return RUN_PROBLEM if report.has_errors else OK


if __name__ == "__main__":
    sys.exit(main())
