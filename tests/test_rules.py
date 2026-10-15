from datetime import date

import pytest

from collection_agent.chasing import Action, ReminderKind
from collection_agent.policy import ChasePolicy
from collection_agent.rules import RuleTag, describe_rule, ordinal
from tests.test_chasing import POLICY, SQ, decide, inv, rem, reply


def tag(
    action: Action,
    i,  # type: ignore[no-untyped-def]
    today: date,
    reminders=None,  # type: ignore[no-untyped-def]
    replies=None,  # type: ignore[no-untyped-def]
    policy: ChasePolicy = POLICY,
) -> RuleTag:
    return describe_rule(action, i, policy, reminders or [], replies or [], today)


def test_no_response() -> None:
    t = tag(Action.REMIND, inv(date(2026, 10, 12)), date(2026, 10, 19))
    assert t == RuleTag(
        "no_response", "No response for 7 days (last contact Monday 12 October 2026)"
    )


def test_no_response_last_contact_is_the_latest_reminder() -> None:
    t = tag(Action.REMIND, inv(date(2026, 10, 1)), date(2026, 10, 15), [rem(date(2026, 10, 8))])
    assert t.label == "No response for 7 days (last contact Thursday 8 October 2026)"


def test_reply_resets_last_contact() -> None:
    t = tag(
        Action.REMIND,
        inv(date(2026, 10, 1)),
        date(2026, 10, 19),
        [rem(date(2026, 10, 8))],
        [reply(date(2026, 10, 11))],
    )
    assert t == RuleTag(
        "no_response", "No response for 7 days (last contact Sunday 11 October 2026)"
    )


def test_no_response_uses_the_policy_days() -> None:
    t = tag(
        Action.REMIND,
        inv(date(2026, 10, 12)),
        date(2026, 10, 22),
        policy=ChasePolicy(no_response_days=10),
    )
    assert t.label.startswith("No response for 10 days (")


def test_pre_deadline_squeeze_scenario() -> None:
    i = inv(SQ)
    r = [rem(date(2026, 11, 5))]
    assert decide(i, date(2026, 11, 11), r) is Action.REMIND_PRE_DEADLINE
    t = tag(Action.REMIND_PRE_DEADLINE, i, date(2026, 11, 11), r)
    assert t == RuleTag(
        "pre_deadline",
        "Payment must clear by the 15th — reminder 3 working days before Monday 16 November 2026",
    )


def test_pre_deadline_uses_policy_values() -> None:
    p = ChasePolicy(clearance_day_of_month=22, pre_deadline_buffer_working_days=5)
    t = tag(Action.REMIND_PRE_DEADLINE, inv(SQ), date(2026, 11, 10), policy=p)
    assert t.label.startswith("Payment must clear by the 22nd — reminder 5 working days before ")


def test_broken_promise() -> None:
    i = inv(date(2026, 10, 1), promised_date=date(2026, 10, 5))
    t = tag(Action.REMIND_BROKEN_PROMISE, i, date(2026, 10, 8))
    assert t == RuleTag(
        "broken_promise",
        "Promised payment by Monday 5 October 2026 not received (2 working days' grace)",
    )


def test_broken_promise_without_a_promised_date_raises() -> None:
    with pytest.raises(ValueError):
        tag(Action.REMIND_BROKEN_PROMISE, inv(date(2026, 10, 1)), date(2026, 10, 8))


def test_escalation_max_reminders() -> None:
    r = [rem(date(2026, 10, 8)), rem(date(2026, 10, 15)), rem(date(2026, 10, 22))]
    t = tag(Action.ESCALATE_TO_HUMAN, inv(date(2026, 10, 1)), date(2026, 10, 29), r)
    assert t == RuleTag("max_reminders", "3 reminders sent without payment — passed to a person")


def test_escalation_below_max_reminders_is_broken_promise_at_max() -> None:
    t = tag(
        Action.ESCALATE_TO_HUMAN,
        inv(date(2026, 10, 1)),
        date(2026, 10, 29),
        [rem(date(2026, 10, 8))],
    )
    assert t.code == "broken_promise_at_max"
    assert t.label == "3 reminders sent without payment — passed to a person"


def test_escalation_uses_policy_max() -> None:
    p = ChasePolicy(max_reminders=2)
    r = [rem(date(2026, 10, 8)), rem(date(2026, 10, 15))]
    t = tag(Action.ESCALATE_TO_HUMAN, inv(date(2026, 10, 1)), date(2026, 10, 22), r, policy=p)
    assert t.label.startswith("2 reminders sent")


@pytest.mark.parametrize(
    ("n", "expected"),
    [
        (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (5, "5th"), (6, "6th"), (7, "7th"),
        (8, "8th"), (9, "9th"), (10, "10th"), (11, "11th"), (12, "12th"), (13, "13th"),
        (14, "14th"), (15, "15th"), (16, "16th"), (17, "17th"), (18, "18th"), (19, "19th"),
        (20, "20th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (24, "24th"), (25, "25th"),
        (26, "26th"), (27, "27th"), (28, "28th"), (29, "29th"), (30, "30th"), (31, "31st"),
    ],
)  # fmt: skip
def test_ordinal(n: int, expected: str) -> None:
    assert ordinal(n) == expected


def test_every_action_has_a_tag_and_unknown_raises() -> None:
    for action in Action:
        if action is Action.REMIND_BROKEN_PROMISE:
            continue
        assert tag(action, inv(date(2026, 10, 1)), date(2026, 10, 8)).code
    with pytest.raises(ValueError):
        tag("nonsense", inv(date(2026, 10, 1)), date(2026, 10, 8))  # type: ignore[arg-type]


def _scenarios() -> list[tuple[object, date, list, list]]:  # type: ignore[type-arg]
    three = [rem(date(2026, 10, 8)), rem(date(2026, 10, 15)), rem(date(2026, 10, 22))]
    return [
        (inv(SQ), date(2026, 11, 11), [rem(date(2026, 11, 5))], []),
        (inv(date(2026, 10, 12)), date(2026, 10, 19), [], []),
        (inv(date(2026, 10, 10)), date(2026, 10, 19), [], []),
        (
            inv(date(2026, 10, 1)),
            date(2026, 10, 19),
            [rem(date(2026, 10, 8))],
            [reply(date(2026, 10, 11))],
        ),
        (inv(date(2026, 10, 1)), date(2026, 10, 29), three, []),
        (inv(date(2026, 10, 1)), date(2026, 11, 5), three, []),
        (inv(date(2026, 10, 1), promised_date=date(2026, 10, 5)), date(2026, 10, 8), [], []),
        (inv(date(2026, 10, 1), promised_date=date(2026, 10, 5)), date(2026, 10, 29), three, []),
        (inv(date(2026, 10, 1), promised_date=date(2026, 10, 5)), date(2026, 10, 8),
         [rem(date(2026, 10, 7), ReminderKind.PRE_DEADLINE)], []),
        (inv(date(2026, 10, 1)), date(2026, 10, 8), [], []),
        (inv(date(2026, 10, 1)), date(2026, 10, 7), [], []),
    ]  # fmt: skip


def test_describe_rule_covers_every_action_decide_returns() -> None:
    seen = set()
    for i, today, reminders, replies in _scenarios():
        action = decide(i, today, reminders, replies)  # type: ignore[arg-type]
        if action is None:
            continue
        seen.add(action)
        assert tag(action, i, today, reminders, replies).code  # type: ignore[arg-type]
    assert seen == set(Action)
