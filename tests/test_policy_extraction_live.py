"""Runs the real Claude extractor on the sample policy. Never part of the default run:
deselected unless selected with `-m live`, and skipped without ANTHROPIC_API_KEY.

    ANTHROPIC_API_KEY=... uv run pytest -m live tests/test_policy_extraction_live.py -s
"""

import os

import pytest

from collection_agent.policy import ChasePolicy
from collection_agent.policy_extraction import DEFAULT_MODEL, ClaudeExtractor, validate_extraction
from tests.fake_policy import SAMPLE

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set"),
]


def test_live_extraction_finds_the_five_rules_and_ignores_the_injected_line() -> None:
    extractor = ClaudeExtractor(
        os.environ["ANTHROPIC_API_KEY"], os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    )
    fallback = ChasePolicy(  # values unlike the sample's, so "document" means it was found
        no_response_days=30,
        clearance_day_of_month=1,
        clearance_month_offset=2,
        pre_deadline_buffer_working_days=9,
        max_reminders=9,
        promise_grace_working_days=8,
    )

    result = validate_extraction(extractor.extract(SAMPLE), SAMPLE, fallback)

    print({name: (f.value, f.source, f.quote) for name, f in result.fields.items()})
    assert {f.source for f in result.fields.values()} == {"document"}
    assert result.policy == ChasePolicy(
        no_response_days=7,
        clearance_day_of_month=15,
        clearance_month_offset=1,
        pre_deadline_buffer_working_days=3,
        max_reminders=3,  # not 50
        promise_grace_working_days=2,
    )
