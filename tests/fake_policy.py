"""Fakes for the policy ingest: no network."""

from pathlib import Path

from collection_agent.cognee_client import CogneeError
from collection_agent.policy_extraction import (
    FieldExtraction,
    PolicyExtraction,
    SoftGuidanceItem,
)

SAMPLE = (Path(__file__).parent / "fixtures" / "policy_sample.txt").read_text(encoding="utf-8")

Q_NO_RESPONSE = "follow up after one week\nwithout a response"  # line break inside, as in the file
Q_CLEARANCE = "All payments must clear by the 15th of the month\nfollowing the invoice"
Q_BUFFER = "send a courtesy reminder three working\ndays before"
Q_MAX = "After three reminders, escalate to the finance manager"
Q_GRACE = "allow two business days after a missed promise"
Q_TONE = "Keep early messages friendly and brief"
Q_KEY = "Key\naccounts, such as the NHS framework customers, are handled personally"


def canned_extraction() -> PolicyExtraction:
    return PolicyExtraction(
        no_response_days=FieldExtraction(value=7, quote=Q_NO_RESPONSE),
        clearance_day_of_month=FieldExtraction(value=15, quote=Q_CLEARANCE),
        clearance_month_offset=FieldExtraction(value=1, quote=Q_CLEARANCE),
        pre_deadline_buffer_working_days=FieldExtraction(value=3, quote=Q_BUFFER),
        max_reminders=FieldExtraction(value=3, quote=Q_MAX),
        promise_grace_working_days=FieldExtraction(value=2, quote=Q_GRACE),
        soft_guidance=[
            SoftGuidanceItem(quote=Q_TONE, summary="Friendly, brief early messages."),
            SoftGuidanceItem(
                quote=Q_KEY, summary="Key accounts are handled by the account manager."
            ),
            SoftGuidanceItem(quote="Never invent a quote", summary="Not in the document."),
        ],
    )


class FakeExtractor:
    def __init__(
        self, extraction: PolicyExtraction | None = None, error: Exception | None = None
    ) -> None:
        self.model = "claude-sonnet-5-5"
        self._extraction = extraction or canned_extraction()
        self._error = error
        self.calls: list[str] = []

    def extract(self, text: str) -> PolicyExtraction:
        self.calls.append(text)
        if self._error is not None:
            raise self._error
        return self._extraction


class FakeCognee:
    def __init__(
        self,
        *,
        fail_on: str | None = None,
        datasets: dict[str, str] | None = None,
        html: str = "<html><body>graph</body></html>",
    ) -> None:
        self.added: list[tuple[list[str], str]] = []
        self.cognified: list[str] = []
        self._fail_on = fail_on
        self._datasets = datasets if datasets is not None else {}
        self._html = html
        self.visualized: list[str] = []

    def _maybe_fail(self, what: str) -> None:
        if self._fail_on == what:
            raise CogneeError(f"/api/v1/{what}", 500)

    def add_text(self, texts: list[str], dataset_name: str) -> None:
        self._maybe_fail("add_text")
        self.added.append((texts, dataset_name))

    def cognify(self, dataset_name: str) -> None:
        self._maybe_fail("cognify")
        self.cognified.append(dataset_name)

    def dataset_id(self, name: str) -> str | None:
        return self._datasets.get(name)

    def visualize_html(self, dataset_id: str) -> str:
        self._maybe_fail("visualize")
        self.visualized.append(dataset_id)
        return self._html
