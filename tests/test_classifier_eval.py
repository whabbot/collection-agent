"""The eval report and the `eval-classifier` command, with a fake classifier: no network."""

from pathlib import Path

import pytest

from collection_agent.classifier import (
    Classification,
    ClassifierError,
    ClassifierFacts,
    Intent,
)
from collection_agent.classifier_eval import load_cases, run_eval
from collection_agent.cli import USAGE_OR_CONFIG, main
from tests.fake_classifier import FakeClassifier, absolute, make_classification, relative

FIXTURES = Path(__file__).parent / "fixtures" / "replies.jsonl"
CASES = load_cases(FIXTURES)
EXPECTED = {c.text: c for c in CASES}


def perfect(text: str) -> Classification:
    case = EXPECTED[text]
    return make_classification(case.expected_intent)


def test_perfect_classifier_scores_everything_and_has_no_confusions() -> None:
    report = run_eval(FakeClassifier(by_text=perfect), CASES)
    assert report.total == len(CASES)
    assert report.confusions == []
    assert all(ok == n for ok, n in report.per_intent.values())
    assert set(report.per_intent) == {i.value for i in Intent}


def test_confusions_and_errors_are_reported() -> None:
    def sometimes_wrong(text: str) -> Classification:
        case = EXPECTED[text]
        if case.id == "dispute-1":
            return make_classification(Intent.OTHER)
        if case.id == "ooo-1":
            raise ClassifierError("down")
        return perfect(text)

    report = run_eval(FakeClassifier(by_text=sometimes_wrong), CASES)
    assert ("dispute-1", "dispute", "other") in report.confusions
    assert any(e.startswith("ooo-1") for e in report.errors)
    assert report.per_intent["dispute"][0] == report.per_intent["dispute"][1] - 1
    text = report.render()
    assert "dispute-1: dispute -> other" in text
    assert "error ooo-1" in text


def test_date_accuracy_assembles_dates_in_code() -> None:
    def with_dates(text: str) -> Classification:
        case = EXPECTED[text]
        if case.id == "promise-2":  # "Payment will be made on Friday."
            return make_classification(case.expected_intent, date_parts=relative("friday"))
        if case.id == "promise-4":  # "30 October"
            return make_classification(case.expected_intent, date_parts=absolute("october", 29))
        return make_classification(case.expected_intent)

    report = run_eval(FakeClassifier(by_text=with_dates), CASES)
    assert report.date_checked >= 8
    assert report.date_correct == 2  # Friday right, "no date" rightly None; the 29th wrong
    assert any(m.startswith("promise-4") for m in report.date_misses)
    assert "date accuracy" in report.render()


def test_option_order_flips_are_reported_for_five_cases() -> None:
    def flip_on_one(text: str) -> Classification:
        return make_classification(
            Intent.OTHER if EXPECTED[text].id == "paid-1" else Intent.DISPUTE
        )

    normal = FakeClassifier(by_text=lambda t: make_classification(Intent.DISPUTE))
    reversed_ = FakeClassifier(by_text=flip_on_one)
    report = run_eval(normal, CASES, reversed_)
    assert report.flip_checked == 5
    assert len(reversed_.calls) == 5
    assert all(a == "dispute" for _, a, _ in report.flips)
    assert "option-order flips" in report.render()


def test_cli_prints_the_report_without_a_database(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    fake = FakeClassifier(by_text=perfect)
    code = main(["eval-classifier", "--fixtures", str(FIXTURES)], classifiers=(fake, fake))
    out = capsys.readouterr().out
    assert code == 0
    assert f"cases: {len(CASES)}" in out
    assert "per-intent accuracy" in out


def test_cli_needs_the_api_key(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    code = main(["eval-classifier", "--fixtures", str(FIXTURES)])
    assert code == USAGE_OR_CONFIG
    assert "TYPESAFE_API_KEY" in capsys.readouterr().err


def test_cli_reports_unreadable_fixtures(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    fake = FakeClassifier()
    code = main(
        ["eval-classifier", "--fixtures", str(tmp_path / "nope.jsonl")], classifiers=(fake, fake)
    )
    assert code == USAGE_OR_CONFIG
    assert fake.calls == []


def test_facts_for_a_case_use_its_own_date() -> None:
    case = CASES[0]
    facts: ClassifierFacts = case.facts()
    assert facts.today == case.today
    assert facts.due_on > facts.today
