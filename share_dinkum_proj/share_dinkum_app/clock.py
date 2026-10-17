"""The date that reports are made as at.

`REPORTING_DATE` in settings pins it, as a date or an ISO string; left as None it is today. Tests
override it so figures do not move as the current year ends. Only reporting reads it: fetching
prices, and the timers that compare against stored timestamps, keep the real clock.
"""
from datetime import date

from django.conf import settings


def reporting_date() -> date:
    pinned = getattr(settings, 'REPORTING_DATE', None)
    if pinned is None:
        return date.today()
    return date.fromisoformat(str(pinned))
