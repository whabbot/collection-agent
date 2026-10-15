from datetime import date

import pytest
from pydantic import ValidationError

from collection_agent.calendar import WorkingCalendar
from collection_agent.policy import ChasePolicy

CAL = WorkingCalendar(frozenset())


def test_defaults() -> None:
    p = ChasePolicy()
    assert p.no_response_days == 7
    assert p.clearance_day_of_month == 15
    assert p.clearance_month_offset == 1
    assert p.pre_deadline_buffer_working_days == 3
    assert p.max_reminders == 3
    assert p.promise_grace_working_days == 2


def test_frozen() -> None:
    with pytest.raises(ValidationError):
        ChasePolicy().max_reminders = 5  # type: ignore[misc]


def test_clearance_deadline_rolls_off_sunday() -> None:
    assert ChasePolicy().clearance_deadline(date(2026, 10, 12), CAL) == date(2026, 11, 16)


def test_clearance_deadline_december_rolls_to_january() -> None:
    assert ChasePolicy().clearance_deadline(date(2026, 12, 3), CAL) == date(2027, 1, 15)


def test_clearance_deadline_offset_two() -> None:
    p = ChasePolicy(clearance_month_offset=2)
    assert p.clearance_deadline(date(2026, 11, 20), CAL) == date(2027, 1, 15)


@pytest.mark.parametrize("day", [0, 29, -1])
def test_rejects_bad_clearance_day(day: int) -> None:
    with pytest.raises(ValidationError):
        ChasePolicy(clearance_day_of_month=day)


@pytest.mark.parametrize(
    "field",
    [
        "no_response_days",
        "clearance_month_offset",
        "pre_deadline_buffer_working_days",
        "max_reminders",
        "promise_grace_working_days",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_rejects_non_positive(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        ChasePolicy(**{field: value})
