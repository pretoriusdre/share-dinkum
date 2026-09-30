"""The account holder's residency history, and day counting over it.

Used for discount apportionment (s115-105, s115-115), disregarding gains (s855-10) and
indexation (s114-25). With no periods declared the basis is `LEGACY`: the flat discount,
reported as an assumption. Declaring periods switches it to `DIVISION_115`.
"""

from datetime import date, timedelta
from typing import TYPE_CHECKING

from share_dinkum_app.choices import CGTBasis, ResidencyStatus

#: Re-exported from ResidencyStatus.
if TYPE_CHECKING:
    from share_dinkum_app.models import Account, ResidencyPeriod

Periods = list['ResidencyPeriod']

RESIDENT = ResidencyStatus.RESIDENT
FOREIGN = ResidencyStatus.FOREIGN
TEMPORARY = ResidencyStatus.TEMPORARY

#: Residency has been declared, so Division 115 apportionment applies.
BASIS_DIVISION_115 = CGTBasis.DIVISION_115

#: Residency has not been declared. The flat rate is applied and flagged as an assumption.
BASIS_LEGACY = CGTBasis.LEGACY

#: Days abroad on or before this date do not reduce the discount (s115-115).
APPORTIONMENT_START_DATE = date(2012, 5, 8)


def periods(account: 'Account | None') -> Periods:
    """Declared residency periods for an account, earliest first."""
    from share_dinkum_app.models import ResidencyPeriod

    if account is None:
        return []
    return list(
        ResidencyPeriod.objects.filter(account=account, is_active=True).order_by('start_date')
    )


def basis(account: 'Account | None', declared: Periods | None = None) -> str:
    """DIVISION_115 if any residency is declared, else LEGACY."""
    if declared is None:
        declared = periods(account)
    return BASIS_DIVISION_115 if declared else BASIS_LEGACY


def status_on(account: 'Account | None', day: date | None, declared: Periods | None = None) -> str | None:
    """Residency status on one day, or None where it was never declared."""
    if day is None:
        return None
    if declared is None:
        declared = periods(account)
    for period in declared:
        if period.covers(day):
            return period.status
    return None


def _clip(period: 'ResidencyPeriod', start: date, end: date) -> tuple[date, date] | None:
    """The part of a declared period lying inside [start, end], or None."""
    period_start = period.start_date
    period_end = period.end_date if period.end_date is not None else date.max
    lower = max(period_start, start)
    upper = min(period_end, end)
    if lower > upper:
        return None
    return lower, upper


def _inclusive_days(start: date | None, end: date | None) -> int:
    if start is None or end is None or end < start:
        return 0
    return (end - start).days + 1


def days_by_status(account: 'Account | None', start: date | None, end: date | None,
                   declared: Periods | None = None) -> dict[str | None, int]:
    """Days in [start, end], both inclusive (s115-105(2)(d)), counted by status.

    Days no period covers are counted under None.
    """
    counts: dict[str | None, int] = {}
    if start is None or end is None or end < start:
        return counts
    if declared is None:
        declared = periods(account)

    covered = 0
    for period in declared:
        window = _clip(period, start, end)
        if window is None:
            continue
        days = _inclusive_days(*window)
        counts[period.status] = counts.get(period.status, 0) + days
        covered += days

    total = _inclusive_days(start, end)
    if covered < total:
        counts[None] = counts.get(None, 0) + (total - covered)
    return counts


def resident_days(account: 'Account | None', start: date | None, end: date | None, declared: Periods | None = None) -> int:
    """Days of Australian residency in [start, end]."""
    return days_by_status(account, start, end, declared=declared).get(RESIDENT, 0)


def non_resident_days(account: 'Account | None', start: date | None, end: date | None, declared: Periods | None = None) -> int:
    """Days of foreign or temporary residency in [start, end] (s115-105(2)(e))."""
    counts = days_by_status(account, start, end, declared=declared)
    return counts.get(FOREIGN, 0) + counts.get(TEMPORARY, 0)


def undeclared_days(account: 'Account | None', start: date | None, end: date | None, declared: Periods | None = None) -> int:
    """Days in [start, end] that no declared period covers."""
    return days_by_status(account, start, end, declared=declared).get(None, 0)


def days_not_resident(account: 'Account | None', start: date | None, end: date | None, declared: Periods | None = None) -> int:
    """Days in [start, end] not known to be Australian resident: foreign, temporary or
    undeclared.

    What the discount apportionment counts against, so a gap in the history reduces the
    discount rather than being taken as residency.
    """
    return _inclusive_days(start, end) - resident_days(account, start, end, declared=declared)


def has_non_resident_days_after_cutoff(account: 'Account | None', start: date | None, end: date | None,
                                       declared: Periods | None = None) -> bool:
    """The s115-105(2)(e) test: any foreign or temporary day in [start, end] after 8 May 2012.

    Tests the whole ownership period, so a returned expatriate stays caught.
    """
    if start is None or end is None:
        return False
    window_start = max(start, APPORTIONMENT_START_DATE + timedelta(days=1))
    return non_resident_days(account, window_start, end, declared=declared) > 0


def first_departure(account: 'Account | None', declared: Periods | None = None) -> 'ResidencyPeriod | None':
    """The first non-resident period that follows a resident one (a departure), or None."""
    if declared is None:
        declared = periods(account)
    previous = None
    for period in declared:
        if period.status != RESIDENT and previous is not None and previous.status == RESIDENT:
            return period
        previous = period
    return None


def coverage_problems(account: 'Account | None', declared: Periods | None = None) -> list[str]:
    """Problems with the saved residency history, as sentences; empty if none or undeclared.

    Checks for gaps, an open-ended period followed by another, a start after the earliest
    buy, and an end before today. Needed because imports skip `ResidencyPeriod.clean()`.
    """
    from share_dinkum_app.models import Buy

    if declared is None:
        declared = periods(account)
    if not declared:
        return []

    problems: list[str] = []
    for earlier, later in zip(declared, declared[1:]):
        if earlier.end_date is None:
            problems.append(
                f'{earlier} is open ended but {later} starts afterwards, so two statuses '
                f'overlap or one has no end.')
        elif (later.start_date - earlier.end_date).days != 1:
            problems.append(f'Residency is not declared between {earlier} and {later}.')

    earliest_buy = Buy.objects.filter(account=account, is_active=True).order_by('date').first()
    if earliest_buy and declared[0].start_date > earliest_buy.date:
        problems.append(
            f'Residency is declared only from {declared[0].start_date.isoformat()}, but the '
            f'earliest purchase was on {earliest_buy.date.isoformat()}.')

    last = declared[-1]
    if last.end_date is not None and last.end_date < date.today():
        problems.append(
            f'Residency is declared only up to {last.end_date.isoformat()}. The days since '
            f'count as not resident: they deny indexation and reduce the discount. Add a '
            f'period for where you have been resident since.')

    return problems
