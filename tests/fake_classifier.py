"""In-memory ReplyClassifier. Never touches the network."""

from collections.abc import Callable

from collection_agent.classifier import (
    AdminSubtype,
    Classification,
    ClassifierError,
    ClassifierFacts,
    Intent,
)


def parts(**kw: str) -> dict[str, str]:
    base = {
        "date_form": "none",
        "month": "not_stated",
        "day_of_month": "not_stated",
        "relative_anchor": "not_stated",
    }
    return {**base, **kw}


def absolute(month: str, day: int) -> dict[str, str]:
    return parts(date_form="absolute", month=month, day_of_month=str(day))


def relative(anchor: str) -> dict[str, str]:
    return parts(date_form="relative", relative_anchor=anchor)


def make_classification(
    intent: Intent = Intent.ALREADY_PAID,
    confidence: float = 0.9,
    admin_subtype: AdminSubtype | None = None,
    date_parts: dict[str, str] | None = None,
) -> Classification:
    return Classification(
        intent=intent,
        confidence=confidence,
        probabilities={intent.value: confidence},
        admin_subtype=admin_subtype,
        promised_date_parts=date_parts if date_parts is not None else parts(),
        model="jev-test",
    )


class FakeClassifier:
    def __init__(
        self,
        classification: Classification | None = None,
        *,
        error: Exception | None = None,
        by_text: Callable[[str], Classification] | None = None,
    ) -> None:
        self.classification = classification or make_classification()
        self.error = error
        self.by_text = by_text
        self.calls: list[tuple[str, ClassifierFacts]] = []

    def classify(self, reply_text: str, facts: ClassifierFacts) -> Classification:
        self.calls.append((reply_text, facts))
        if self.error is not None:
            raise self.error
        return self.by_text(reply_text) if self.by_text else self.classification


def failing() -> FakeClassifier:
    return FakeClassifier(error=ClassifierError("boom"))
