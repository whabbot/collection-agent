"""Accuracy report for the reply classifier over a file of synthetic replies. This is the
only code that runs the classifier over many emails; it needs a real key and is never part of
the default test run (`collection-agent eval-classifier`, or `pytest -m live`)."""

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from collection_agent.classifier import (
    ClassifierError,
    ClassifierFacts,
    Intent,
    ReplyClassifier,
    assemble_promised_date,
)

DEFAULT_FIXTURES = Path("tests/fixtures/replies.jsonl")
FLIP_SAMPLE = 5
FATAL_STATUS_CODES = (401, 402, 403)  # bad key, no credits, no access: every later case fails too
_UNSET = object()


@dataclass(frozen=True)
class EvalCase:
    id: str
    text: str
    expected_intent: Intent
    today: date
    injection: bool = False
    expected_admin_subtype: str | None = None
    has_expected_date: bool = False
    expected_promised_date: date | None = None

    def facts(self) -> ClassifierFacts:
        return ClassifierFacts(
            reference="INV-1001",
            amount="£1,200.00",
            due_on=self.today + timedelta(days=30),
            today=self.today,
        )


def load_cases(path: Path) -> list[EvalCase]:
    cases: list[EvalCase] = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        expected = raw.get("expected_promised_date", _UNSET)
        cases.append(
            EvalCase(
                id=raw.get("id", f"line-{number}"),
                text=raw["text"],
                expected_intent=Intent(raw["expected_intent"]),
                today=date.fromisoformat(raw["today"]),
                injection=bool(raw.get("injection", False)),
                expected_admin_subtype=raw.get("expected_admin_subtype"),
                has_expected_date=expected is not _UNSET,
                expected_promised_date=(
                    date.fromisoformat(expected) if isinstance(expected, str) else None
                ),
            )
        )
    return cases


@dataclass
class EvalReport:
    total: int = 0
    errors: list[str] = field(default_factory=list)
    per_intent: dict[str, tuple[int, int]] = field(default_factory=dict)  # intent -> (ok, n)
    confusions: list[tuple[str, str, str]] = field(default_factory=list)  # (id, expected, got)
    date_checked: int = 0
    date_correct: int = 0
    date_misses: list[str] = field(default_factory=list)
    flips: list[tuple[str, str, str]] = field(default_factory=list)  # (id, normal, reversed)
    flip_checked: int = 0
    fatal: str | None = None  # set when the run stopped early on an auth or billing error

    def render(self) -> str:
        lines = [f"cases: {self.total}   classifier errors: {len(self.errors)}"]
        lines.append("per-intent accuracy:")
        for intent in Intent:
            ok, n = self.per_intent.get(intent.value, (0, 0))
            if n:
                lines.append(f"  {intent.value:<22} {ok}/{n}  ({100 * ok / n:.0f}%)")
        lines.append("confusions (id: expected -> got):")
        lines.extend(f"  {i}: {e} -> {g}" for i, e, g in self.confusions)
        if not self.confusions:
            lines.append("  none")
        if self.date_checked:
            pct = 100 * self.date_correct / self.date_checked
            lines.append(f"date accuracy: {self.date_correct}/{self.date_checked} ({pct:.0f}%)")
            lines.extend(f"  miss {m}" for m in self.date_misses)
        lines.append(f"option-order flips: {len(self.flips)}/{self.flip_checked} re-run")
        lines.extend(f"  {i}: {a} vs {b} (reversed)" for i, a, b in self.flips)
        lines.extend(f"error {e}" for e in self.errors)
        return "\n".join(lines)


def run_eval(
    classifier: ReplyClassifier,
    cases: list[EvalCase],
    reversed_classifier: ReplyClassifier | None = None,
) -> EvalReport:
    report = EvalReport(total=len(cases))

    def stop(case_id: str, exc: ClassifierError) -> bool:
        if exc.status_code in FATAL_STATUS_CODES:
            report.fatal = (
                f"stopped at {case_id}: the provider rejected the request "
                f"(HTTP {exc.status_code}); check the API key and billing"
            )
            return True
        return False

    tally: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    got_by_id: dict[str, str] = {}
    for case in cases:
        tally[case.expected_intent.value][1] += 1
        try:
            result = classifier.classify(case.text, case.facts())
        except ClassifierError as exc:
            if stop(case.id, exc):
                return report
            report.errors.append(f"{case.id}: {exc}")
            continue
        got_by_id[case.id] = result.intent.value
        if result.intent is case.expected_intent:
            tally[case.expected_intent.value][0] += 1
        else:
            report.confusions.append((case.id, case.expected_intent.value, result.intent.value))
        if case.has_expected_date:
            report.date_checked += 1
            assembled = assemble_promised_date(result.promised_date_parts, case.today)
            if assembled == case.expected_promised_date:
                report.date_correct += 1
            else:
                report.date_misses.append(
                    f"{case.id}: expected {case.expected_promised_date} got {assembled}"
                )
    report.per_intent = {k: (v[0], v[1]) for k, v in tally.items()}

    if reversed_classifier is not None and cases:
        step = max(1, len(cases) // FLIP_SAMPLE)
        for case in cases[::step][:FLIP_SAMPLE]:
            if case.id not in got_by_id:
                continue
            report.flip_checked += 1
            try:
                other = reversed_classifier.classify(case.text, case.facts()).intent.value
            except ClassifierError as exc:
                if stop(case.id, exc):
                    return report
                report.errors.append(f"{case.id} (reversed): {exc}")
                continue
            if other != got_by_id[case.id]:
                report.flips.append((case.id, got_by_id[case.id], other))
    return report
