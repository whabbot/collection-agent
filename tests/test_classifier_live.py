"""Runs the real Jev classifier over the synthetic replies. Never part of the default run:
deselected unless selected with `-m live`, and skipped without TYPESAFE_API_KEY.

    TYPESAFE_API_KEY=... uv run pytest -m live -s
"""

import os
from pathlib import Path

import pytest

from collection_agent.classifier import Intent, JevClassifier
from collection_agent.classifier_eval import load_cases, run_eval

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("TYPESAFE_API_KEY"), reason="TYPESAFE_API_KEY not set"),
]

FIXTURES = Path(__file__).parent / "fixtures" / "replies.jsonl"


def test_live_eval_over_the_synthetic_replies() -> None:
    key = os.environ["TYPESAFE_API_KEY"]
    model = os.environ.get("JEV_MODEL", "jev-1.13.0")
    cases = load_cases(FIXTURES)
    report = run_eval(
        JevClassifier(key, model),
        cases,
        JevClassifier(key, model, intent_order=list(reversed(Intent))),
    )
    print("\n" + report.render())
    assert report.total == len(cases)  # it ran; accuracy is read from the report, not asserted
