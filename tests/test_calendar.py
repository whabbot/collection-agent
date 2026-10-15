from datetime import date

import pytest

from collection_agent.calendar import WorkingCalendar, uk_calendar

XMAS = date(2026, 12, 25)  # Fri
BOXING_SUB = date(2026, 12, 28)  # Mon, substitute Boxing Day
CAL = WorkingCalendar(frozenset({XMAS, BOXING_SUB}))


def test_weekends_are_not_working_days() -> None:
    assert not CAL.is_working_day(date(2026, 10, 17))
    assert not CAL.is_working_day(date(2026, 10, 18))
    assert CAL.is_working_day(date(2026, 10, 19))


def test_holiday_is_not_working_day() -> None:
    assert not CAL.is_working_day(XMAS)


def test_roll_forward_working_day_unchanged() -> None:
    assert CAL.roll_forward(date(2026, 10, 16)) == date(2026, 10, 16)


def test_roll_forward_over_weekend() -> None:
    assert CAL.roll_forward(date(2026, 11, 15)) == date(2026, 11, 16)


def test_roll_forward_over_bank_holiday() -> None:
    assert CAL.roll_forward(XMAS) == date(2026, 12, 29)


def test_add_working_days_forward_across_weekend() -> None:
    assert CAL.add_working_days(date(2026, 11, 6), 2) == date(2026, 11, 10)


def test_add_working_days_back_across_weekend() -> None:
    assert CAL.add_working_days(date(2026, 11, 16), -3) == date(2026, 11, 11)


def test_add_working_days_across_christmas() -> None:
    assert CAL.add_working_days(date(2026, 12, 24), 1) == date(2026, 12, 29)
    assert CAL.add_working_days(date(2026, 12, 29), -1) == date(2026, 12, 24)


def test_add_working_days_zero_is_identity() -> None:
    assert CAL.add_working_days(date(2026, 11, 14), 0) == date(2026, 11, 14)


def test_add_working_days_from_non_working_day() -> None:
    # Counting only working days: Sat + 1 working day = Monday.
    assert CAL.add_working_days(date(2026, 11, 14), 1) == date(2026, 11, 16)
    assert CAL.add_working_days(date(2026, 11, 14), -1) == date(2026, 11, 13)


def test_uk_calendar_christmas_2026() -> None:
    cal = uk_calendar([2026])
    assert not cal.is_working_day(XMAS)
    assert not cal.is_working_day(BOXING_SUB)
    assert cal.is_working_day(date(2026, 12, 29))


def test_uk_calendar_is_immutable() -> None:
    cal = uk_calendar([2026])
    with pytest.raises(AttributeError):
        cal.holidays = frozenset()  # type: ignore[misc]
