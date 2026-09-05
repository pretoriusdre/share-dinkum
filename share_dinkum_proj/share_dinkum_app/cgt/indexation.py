"""Division 114 and Subdivision 960-M: indexing a cost base to CPI.

From 1 July 2027 an individual or trust no longer halves a capital gain. Instead the cost
base grows with inflation, so only the real gain is taxed. The two are mutually exclusive
and there is no election between them: s110-36(1A) makes indexation mandatory ("the cost
base *also includes* indexation ... if" the Division 114 conditions are met), and s115-20
then denies the discount to any gain "worked out using a cost base that has been calculated
with reference to indexation". Nothing here asks the user which they would prefer, because
the Act does not offer the choice.

Three conditions have to hold together, and each fails in a different direction:

* **s114-25** -- the holder must have been neither a foreign nor a temporary resident at any
  time in the testing period, which starts on the *later* of 1 July 2027 and the day of
  acquisition. Residency before the cutover is irrelevant, which is the opposite of the
  intuitive reading and is tested for that reason.
* **s114-30** -- the asset test.
* **s960-275(1B)** -- the index runs only from the quarter starting 1 July 2027, so growth
  before then is never indexed, however long the asset has been held.

**Where the data is missing, this raises.** A missing CPI quarter has no safe default. Using
the latest published quarter understates the cost base and so overstates the gain;
extrapolating does the reverse. Both produce a plausible number that is wrong, and a
plausible wrong number on a tax return is worse than a refusal. There is a structural
timing problem behind this: a quarter's index is published several weeks after the quarter
ends, so a disposal in late June cannot be finally indexed until the following month, which
is every user who files early.
"""

from decimal import Decimal, ROUND_HALF_UP

from share_dinkum_app.cgt import residency
from share_dinkum_app.constants import (
    CGT_CUTOVER_DATE,
    CGT_INDEXATION_FIRST_QUARTER,
    CGT_INDEXATION_FLAT_RATE,
    CGT_INDEXATION_METHOD,
)

#: s960-275: the factor is worked out to three decimal places, rounding up at five.
_FACTOR_PRECISION = Decimal('0.001')

#: Below this, indexation gives nothing back and the factor is taken as 1. s960-275(4)
#: expresses the same idea: a factor of less than 1 is disregarded, so deflation never
#: shrinks a cost base.
_NO_INDEXATION = Decimal('1.000')


class IndexationDataUnavailable(Exception):
    """A CPI quarter needed to index a cost base has not been loaded.

    Deliberately an exception rather than a fallback value. A caller has to decide what to
    do about it, and reports mark the row as pending with no relief rather than quietly
    reporting an unindexed cost base as though it were final.
    """


def quarter_start(day):
    """The first day of the CPI quarter containing `day`."""
    if day is None:
        return None
    return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)


def index_number(day):
    """The CPI index number for the quarter containing `day`.

    Raises `IndexationDataUnavailable` where that quarter has not been loaded.
    """
    from share_dinkum_app.models import CPIIndex

    start = quarter_start(day)
    entry = CPIIndex.objects.filter(quarter_start_date=start).first()
    if entry is None:
        raise IndexationDataUnavailable(
            f'No CPI index number for the quarter starting {start.isoformat()}. Load it with '
            f'"manage.py load_cpi" before indexing a cost base. Guessing at it would change '
            f'a figure on a tax return.'
        )
    return entry.index_number


def is_indexation_eligible(account, acquisition_date, event_date, declared=None):
    """The s114-25 testing period test.

    The testing period starts on the later of 1 July 2027 and the day of acquisition, and
    ends on the day of the CGT event. Any foreign or temporary residency inside it denies
    indexation outright -- there is no apportionment and no partial credit, unlike the
    discount it replaces.

    Two consequences worth stating, because both are counter-intuitive:

    * Someone who lived abroad until 2026 and has been in Australia since is **eligible**.
      The testing period never looks back before the cutover, so a past absence costs them
      nothing.
    * Someone who spends a single week abroad in 2028 loses indexation on everything they
      then sell, entirely. One week and ten years are the same answer.
    """
    if event_date is None or event_date < CGT_CUTOVER_DATE:
        return False

    if declared is None:
        declared = residency.periods(account)
    if not declared:
        # Residency has not been declared, so the test cannot be passed. Indexation is not
        # granted on an assumption: unlike the discount, there is no established prior
        # behaviour to preserve here, and granting it would inflate a cost base.
        return False

    start = max(CGT_CUTOVER_DATE, acquisition_date or CGT_CUTOVER_DATE)
    if start > event_date:
        return False

    counts = residency.days_by_status(account, start, event_date, declared=declared)
    disqualifying = (
        counts.get(residency.FOREIGN, 0)
        + counts.get(residency.TEMPORARY, 0)
        + counts.get(None, 0)
    )
    return disqualifying == 0


def indexation_factor(acquisition_date, event_date, method=None):
    """How much a cost base grows between acquisition and disposal.

    A Decimal to three decimal places, never below 1. The acquisition quarter is floored at
    the quarter starting 1 July 2027 by s960-275(1B), so an asset bought in 2010 and sold in
    2035 is indexed over eight years, not twenty-five.
    """
    method = method or CGT_INDEXATION_METHOD

    from_date = max(acquisition_date or CGT_INDEXATION_FIRST_QUARTER,
                    CGT_INDEXATION_FIRST_QUARTER)
    if event_date is None or event_date < from_date:
        return _NO_INDEXATION

    if method == 'FLAT_RATE':
        years = Decimal((event_date - from_date).days) / Decimal('365.25')
        factor = (Decimal('1') + Decimal(str(CGT_INDEXATION_FLAT_RATE))) ** years
    else:
        start = index_number(from_date)
        end = index_number(event_date)
        if not start:
            raise IndexationDataUnavailable(
                f'The CPI index number for the quarter starting '
                f'{quarter_start(from_date).isoformat()} is zero, so no factor can be '
                f'derived from it.')
        factor = Decimal(end) / Decimal(start)

    factor = Decimal(factor).quantize(_FACTOR_PRECISION, rounding=ROUND_HALF_UP)
    return max(factor, _NO_INDEXATION)


def indexed_cost_base(cost_base, acquisition_date, event_date, method=None):
    """A cost base grown by the indexation factor.

    The whole cost base is indexed from one date, which is a simplification the Act does not
    make: s960-275 indexes each element of the cost base from the day that expenditure was
    incurred, so brokerage paid on a later date should strictly be indexed from then. For a
    share parcel the elements are all incurred within days of each other, so the difference
    is immaterial; it would not be for an asset improved over years.
    """
    if cost_base is None:
        return cost_base
    factor = indexation_factor(acquisition_date, event_date, method=method)
    return cost_base * factor
