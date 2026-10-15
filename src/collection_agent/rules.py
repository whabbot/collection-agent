"""Which rule fired: a stable code plus a human label for a chase action. Pure.

Sees the same inputs as `decide_next_action` but never decides anything; the label is built only
from the approved `ChasePolicy`, the invoice's stored dates and the action.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from collection_agent.chasing import Action, CustomerReply, InvoiceSnapshot, ReminderSent
from collection_agent.policy import ChasePolicy
from collection_agent.templates import format_date


@dataclass(frozen=True)
class RuleTag:
    code: str
    label: str


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def describe_rule(
    action: Action,
    inv: InvoiceSnapshot,
    policy: ChasePolicy,
    reminders: Sequence[ReminderSent],
    replies: Sequence[CustomerReply],
    today: date,
) -> RuleTag:
    """Raises ValueError for an action that has no tag."""
    if action is Action.REMIND:
        last_contact = max(
            [inv.sent_on, *(r.sent_on for r in reminders), *(r.received_on for r in replies)]
        )
        return RuleTag(
            "no_response",
            f"No response for {policy.no_response_days} days "
            f"(last contact {format_date(last_contact)})",
        )
    if action is Action.REMIND_PRE_DEADLINE:
        return RuleTag(
            "pre_deadline",
            f"Payment must clear by the {ordinal(policy.clearance_day_of_month)} — "
            f"reminder {policy.pre_deadline_buffer_working_days} working days before "
            f"{format_date(inv.due_on)}",
        )
    if action is Action.REMIND_BROKEN_PROMISE:
        if inv.promised_date is None:
            raise ValueError("a broken-promise reminder needs a promised date")
        return RuleTag(
            "broken_promise",
            f"Promised payment by {format_date(inv.promised_date)} not received "
            f"({policy.promise_grace_working_days} working days' grace)",
        )
    if action is Action.ESCALATE_TO_HUMAN:
        code = (
            "max_reminders" if len(reminders) >= policy.max_reminders else "broken_promise_at_max"
        )
        return RuleTag(
            code,
            f"{policy.max_reminders} reminders sent without payment — passed to a person",
        )
    raise ValueError(f"no rule tag for action {action!r}")
