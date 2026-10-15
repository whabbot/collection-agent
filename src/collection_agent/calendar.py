"""Working-day calendar (weekends plus a supplied set of holidays)."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

import holidays

_SATURDAY = 5


@dataclass(frozen=True, slots=True)
class WorkingCalendar:
    """Immutable calendar; Saturdays, Sundays and `holidays` are not working days."""

    holidays: frozenset[date] = frozenset()

    def is_working_day(self, d: date) -> bool:
        return d.weekday() < _SATURDAY and d not in self.holidays

    def roll_forward(self, d: date) -> date:
        """`d` if it is a working day, else the next working day."""
        while not self.is_working_day(d):
            d += timedelta(days=1)
        return d

    def add_working_days(self, d: date, n: int) -> date:
        """Move `n` working days forward (n > 0) or back (n < 0), counting only working days."""
        step = timedelta(days=1 if n > 0 else -1)
        remaining = abs(n)
        while remaining:
            d += step
            if self.is_working_day(d):
                remaining -= 1
        return d


def uk_calendar(years: Iterable[int]) -> WorkingCalendar:
    """England & Wales bank holidays for `years`."""
    uk = holidays.country_holidays("GB", subdiv="ENG", years=list(years))
    return WorkingCalendar(frozenset(uk.keys()))
