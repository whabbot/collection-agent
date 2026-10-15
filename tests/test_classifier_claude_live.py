"""Runs the real Claude classifier over five synthetic replies. Never part of the default run:
deselected unless selected with `-m live`, and skipped without LIVE_ANTHROPIC_API_KEY.

The test session strips ANTHROPIC_API_KEY from the environment, so this test reads the key from
the explicit LIVE_ANTHROPIC_API_KEY variable instead (live tests only):

    LIVE_ANTHROPIC_API_KEY=... uv run pytest -m live tests/test_classifier_claude_live.py -s
"""

import os
from pathlib import Path

import pytest

from collection_agent.classifier import Intent
from collection_agent.classifier_claude import ClaudeClassifier
from collection_agent.classifier_eval import load_cases, run_eval
from collection_agent.policy_extraction import DEFAULT_MODEL

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not os.environ.get("LIVE_ANTHROPIC_API_KEY"), reason="LIVE_ANTHROPIC_API_KEY not set"
    ),
]

FIXTURES = Path(__file__).parent / "fixtures" / "replies.jsonl"
SAMPLE_SIZE = 5


def test_live_claude_classification_over_five_fixtures() -> None:
    key = os.environ["LIVE_ANTHROPIC_API_KEY"]
    model = os.environ.get("LIVE_ANTHROPIC_MODEL", DEFAULT_MODEL)
    all_cases = load_cases(FIXTURES)
    cases = all_cases[:: max(1, len(all_cases) // SAMPLE_SIZE)][:SAMPLE_SIZE]
    report = run_eval(
        ClaudeClassifier(key, model),
        cases,
        ClaudeClassifier(key, model, intent_order=list(reversed(Intent))),
    )
    print("\n" + report.render())
    assert report.total == len(cases)  # it ran; accuracy is read from the report, not asserted
    assert report.fatal is None
