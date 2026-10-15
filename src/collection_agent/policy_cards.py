"""Rule cards: a draft or approved `ChasePolicy`, each rule with where it came from."""

from dataclasses import dataclass
from typing import Any

from collection_agent.db.models import PolicyStatus
from collection_agent.policy import ChasePolicy
from collection_agent.rules import ordinal

CARD_ORDER = (
    "no_response_days",
    "clearance",
    "pre_deadline_buffer_working_days",
    "max_reminders",
    "promise_grace_working_days",
)
_CLEARANCE_FIELDS = ("clearance_day_of_month", "clearance_month_offset")

_LABELS = {
    "no_response_days": "Follow up after {n} days with no response",
    "pre_deadline_buffer_working_days": "Send a reminder {n} working days before the deadline",
    "max_reminders": "Pass to a person after {n} reminders",
    "promise_grace_working_days": "Allow {n} working days' grace after a missed promise to pay",
}


def month_phrase(offset: int) -> str:
    if offset == 0:
        return "the month the invoice is sent"
    if offset == 1:
        return "the following month"
    return f"{offset} months after sending"


def clearance_label(day: int, offset: int) -> str:
    return f"Payment must clear by the {ordinal(day)} of {month_phrase(offset)}"


def label_for(card: str, policy: ChasePolicy) -> str:
    if card == "clearance":
        return clearance_label(policy.clearance_day_of_month, policy.clearance_month_offset)
    return _LABELS[card].format(n=getattr(policy, card))


def _value(card: str, policy: ChasePolicy) -> Any:
    if card == "clearance":
        return {
            "day_of_month": policy.clearance_day_of_month,
            "month_offset": policy.clearance_month_offset,
        }
    return getattr(policy, card)


@dataclass(frozen=True)
class Card:
    field: str
    label: str
    value: Any
    source: str
    quote: str | None
    current_approved_value: Any
    changed: bool

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def _source_and_quote(card: str, fields: dict[str, Any] | None) -> tuple[str, str | None]:
    if fields is None:
        return "manual", None
    names = _CLEARANCE_FIELDS if card == "clearance" else (card,)
    entries = [fields[n] for n in names]
    sources = [e["source"] for e in entries]
    if all(s == "document" for s in sources):
        quotes = list(dict.fromkeys(e["quote"] for e in entries if e.get("quote")))
        return "document", " … ".join(quotes) or None
    # Not wholly from the document: report the first part that was not.
    return next(s for s in sources if s != "document"), None


def build_cards(
    policy: ChasePolicy,
    fields: dict[str, Any] | None,
    approved: ChasePolicy | None,
) -> list[Card]:
    """`fields` is the stored per-field `{value, quote, source}`; None means no extraction
    (the version was created by hand), so every card is `manual`."""
    cards = []
    for name in CARD_ORDER:
        source, quote = _source_and_quote(name, fields)
        current = _value(name, approved) if approved is not None else None
        value = _value(name, policy)
        cards.append(
            Card(
                field=name,
                label=label_for(name, policy),
                value=value,
                source=source,
                quote=quote,
                current_approved_value=current,
                changed=approved is not None and current != value,
            )
        )
    return cards


def render_cards(
    version: int,
    status: PolicyStatus,
    cards: list[Card],
    soft_guidance: list[dict[str, str]],
) -> str:
    lines = [f"Policy version {version} ({status.value})", ""]
    for card in cards:
        flag = " [changed]" if card.changed else ""
        lines.append(f"- {card.label}{flag}")
        if card.quote:
            lines.append(f'    from the document: "{card.quote}"')
        elif card.source != "manual":
            lines.append(f"    not taken from the document ({card.source}); using the fallback")
        else:
            lines.append("    set manually")
    if soft_guidance:
        lines += ["", "Soft guidance (shown, never applied):"]
        for item in soft_guidance:
            lines.append(f'- {item["summary"]} ("{item["quote"]}")')
    return "\n".join(lines)
