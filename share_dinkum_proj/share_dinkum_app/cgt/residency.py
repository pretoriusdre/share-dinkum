"""Where the account holder was, and when.

Nothing else in this package can characterise a gain without an answer here. The CGT
discount is apportioned by residency (s115-105, s115-115), whether a gain can be
disregarded altogether turns on it (s855-10), and so does whether indexation is available
from 2027 (s114-25). One module owns the day counting so those three do not each grow their
own slightly different version of it.

**An account with no declared residency is not assumed to be resident.** It is `LEGACY`:
the flat 50% the application has always applied, reported as an assumption rather than as a
fact. Declaring residency is what switches an account to `DIVISION_115`, and for a taxpayer
who has always lived in Australia the two produce identical figures -- resident days equal
total days -- so the declaration costs such a user nothing and buys everyone else
correctness.
"""

from datetime import date, timedelta

from share_dinkum_app.choices import CGTBasis, ResidencyStatus

#: Re-exported so a caller reasoning about residency does not have to know where the
#: vocabulary is defined.
RESIDENT = ResidencyStatus.RESIDENT
FOREIGN = ResidencyStatus.FOREIGN
TEMPORARY = ResidencyStatus.TEMPORARY

#: Residency has been declared, so Division 115 apportionment applies.
BASIS_DIVISION_115 = CGTBasis.DIVISION_115

#: Residency has not been declared. The flat 50% is applied and said to be an assumption.
BASIS_LEGACY = CGTBasis.LEGACY

#: Foreign and temporary residency before this date does not reduce the discount.
#: s115-115(2) and (3) apportion only over days after 8 May 2012, the date the discount was
#: withdrawn from foreign residents. A gain accrued while abroad in 2005 keeps its full
#: discount, which is why the naive "resident days over total days" formula is wrong for
#: anyone with a long holding.
APPORTIONMENT_START_DATE = date(2012, 5, 8)


def periods(account):
    """Declared residency periods for an account, earliest first."""
    from share_dinkum_app.models import ResidencyPeriod

    if account is None:
        return []
    return list(
        ResidencyPeriod.objects.filter(account=account, is_active=True).order_by('start_date')
    )


def basis(account, declared=None):
    """Whether this account's gains are characterised by declaration or by assumption."""
    if declared is None:
        declared = periods(account)
    return BASIS_DIVISION_115 if declared else BASIS_LEGACY


def status_on(account, day, declared=None):
    """Residency status on one day, or None where it was never declared."""
    if day is None:
        return None
    if declared is None:
        declared = periods(account)
    for period in declared:
        if period.covers(day):
            return period.status
    return None


def _clip(period, start, end):
    """The part of a declared period lying inside [start, end], or None."""
    period_start = period.start_date
    period_end = period.end_date if period.end_date is not None else date.max
    lower = max(period_start, start)
    upper = min(period_end, end)
    if lower > upper:
        return None
    return lower, upper


def _inclusive_days(start, end):
    if start is None or end is None or end < start:
        return 0
    return (end - start).days + 1


def days_by_status(account, start, end, declared=None):
    """How many days in [start, end] fall in each declared status.

    Both endpoints count. s115-105(2)(d) describes the discount testing period as
    "starting on the day you acquired the CGT asset and ending on the day the CGT event
    happens", and the ATO's own apportionment worksheet counts both of those days, so a
    parcel bought and sold on the same day is one day, not none.

    Days not covered by any declared period are returned under None. That should be
    impossible once `ResidencyPeriod.clean()` has run, but a spreadsheet import can reach
    the database without it, and a silent zero there would quietly grant a full discount.
    """
    counts = {}
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


def resident_days(account, start, end, declared=None):
    """Days of Australian residency in [start, end]."""
    return days_by_status(account, start, end, declared=declared).get(RESIDENT, 0)


def non_resident_days(account, start, end, declared=None):
    """Days of foreign or temporary residency in [start, end].

    Temporary residents count with foreign residents: s115-105(2)(e) reaches anyone who was
    "a foreign resident or a temporary resident", and the apportionment in s115-115 is
    driven by the days that were *not* days of Australian residency.
    """
    counts = days_by_status(account, start, end, declared=declared)
    return counts.get(FOREIGN, 0) + counts.get(TEMPORARY, 0)


def undeclared_days(account, start, end, declared=None):
    """Days in [start, end] that no declared period covers."""
    return days_by_status(account, start, end, declared=declared).get(None, 0)


def has_non_resident_days_after_cutoff(account, start, end, declared=None):
    """The s115-105(2)(e) test: any foreign or temporary residency after 8 May 2012.

    This is the switch that decides whether apportionment happens at all. It is deliberately
    a test over the *whole* ownership period rather than over the year of sale, which is why
    a returned expatriate stays caught by it for as long as they hold the asset.
    """
    if start is None or end is None:
        return False
    window_start = max(start, APPORTIONMENT_START_DATE + timedelta(days=1))
    return non_resident_days(account, window_start, end, declared=declared) > 0


def first_departure(account, declared=None):
    """The first move from Australian residency to foreign or temporary residency.

    Returns the period that begins the absence, or None. This is what s104-165 hangs on:
    leaving Australia triggers CGT event I1, and the choice made then decides whether the
    assets held at that moment stay inside the Australian net.
    """
    if declared is None:
        declared = periods(account)
    previous = None
    for period in declared:
        if period.status != RESIDENT and previous is not None and previous.status == RESIDENT:
            return period
        previous = period
    return None


def coverage_problems(account, declared=None):
    """Ways a saved residency history fails to answer the questions asked of it.

    Returns a list of sentences, empty when the history is sound. This exists because the
    strict validation in `ResidencyPeriod.clean()` only runs behind a form: an Excel import
    writes rows directly, and a set of rows that is individually valid can still leave a
    hole. Reporting the hole is the point -- a gain whose residency is unknown is marked as
    such rather than quietly given a full discount.
    """
    from share_dinkum_app.models import Buy

    if declared is None:
        declared = periods(account)
    if not declared:
        return []

    problems = []
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

    return problems
