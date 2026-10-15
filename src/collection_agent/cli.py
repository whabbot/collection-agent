"""Command line: the daily run and ChasePolicy management.

Exit codes for `run-daily`: 0 success; 1 no approved policy or missing required settings;
2 an invoice ended in `stripe_error` or an unexpected error (or the run itself failed).
Messages name environment variables, never their values.
"""

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from collection_agent.classifier import Intent, JevClassifier, ReplyClassifier
from collection_agent.classifier_eval import DEFAULT_FIXTURES, load_cases, run_eval
from collection_agent.daily import run_daily
from collection_agent.db.engine import make_engine, make_session_factory
from collection_agent.db.repository import (
    NoApprovedPolicyError,
    PolicyStateError,
    approve_policy,
    create_draft_policy,
    get_approved_policy,
)
from collection_agent.deps import (
    EmailIdentity,
    get_today,
    identity_from_settings,
    missing_identity_settings,
)
from collection_agent.policy import ChasePolicy
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
    approve = psub.add_parser("approve", help="approve a draft policy version")
    approve.add_argument("--version", type=int, required=True)
    approve.add_argument("--by", required=True, help="who is approving")

    ev = sub.add_parser(
        "eval-classifier",
        help="run the Jev classifier over the synthetic replies (needs TYPESAFE_API_KEY)",
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
        return _policy(args, factory)
    return _run_daily(args, settings, factory, gateway)


def _eval_classifier(
    args: argparse.Namespace, classifiers: tuple[ReplyClassifier, ReplyClassifier] | None
) -> int:
    """Needs no database. Real classifiers are built only here, from TYPESAFE_API_KEY."""
    if classifiers is None:
        ts = TypesafeSettings()
        if not ts.typesafe_api_key:
            _err("missing required settings: TYPESAFE_API_KEY")
            return USAGE_OR_CONFIG
        reverse = list(reversed(Intent))
        classifiers = (
            JevClassifier(ts.typesafe_api_key, ts.jev_model),
            JevClassifier(ts.typesafe_api_key, ts.jev_model, intent_order=reverse),
        )
    try:
        cases = load_cases(args.fixtures)
    except (OSError, ValueError, KeyError) as exc:
        _err(f"could not read fixtures: {type(exc).__name__}")
        return USAGE_OR_CONFIG
    print(run_eval(classifiers[0], cases, classifiers[1]).render())
    return OK


def _policy(args: argparse.Namespace, factory: sessionmaker[Session]) -> int:
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

    run_date = args.date or get_today()
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
