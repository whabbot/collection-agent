"""Typed chase policy: the hard rules the scheduler reads."""

from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from collection_agent.calendar import WorkingCalendar


class ChasePolicy(BaseModel):
    model_config = ConfigDict(frozen=True)

    no_response_days: int = Field(default=7, gt=0)
    clearance_day_of_month: int = Field(default=15, ge=1, le=28)
    clearance_month_offset: int = Field(default=1, gt=0)
    pre_deadline_buffer_working_days: int = Field(default=3, gt=0)
    max_reminders: int = Field(default=3, gt=0)
    promise_grace_working_days: int = Field(default=2, gt=0)

    def clearance_deadline(self, sent_on: date, cal: WorkingCalendar) -> date:
        months = sent_on.year * 12 + (sent_on.month - 1) + self.clearance_month_offset
        year, month0 = divmod(months, 12)
        return cal.roll_forward(date(year, month0 + 1, self.clearance_day_of_month))
