"""Division 114 and Subdivision 960-M: indexing a cost base to CPI from 1 July 2027.

Where available, indexation is mandatory (s110-36(1A)) and removes the discount (s115-20).

* s114-25: no foreign or temporary residency from the later of 1 July 2027 and acquisition
  to the sale. Earlier residency is irrelevant.
* s960-275(1B): indexed only from the quarter starting 1 July 2027.
* The s114-30 asset test is not checked.

A missing CPI quarter raises `IndexationDataUnavailable` rather than guessing.
"""

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import TYPE_CHECKING, Any, overload

from share_dinkum_app.choices import TaxpayerType
from share_dinkum_app.cgt import discount, residency
from share_dinkum_app.constants import (
    CGT_CUTOVER_DATE,
    CGT_INDEXATION_FIRST_QUARTER,
    CGT_INDEXATION_FLAT_RATE,
    CGT_INDEXATION_METHOD,
)

#: s960-275: the factor is worked out to three decimal places, rounding up at five.
if TYPE_CHECKING:
    from share_dinkum_app.models import Account

_FACTOR_PRECISION = Decimal('0.001')

#: The minimum factor, so deflation never shrinks a cost base (s960-275(4)).
_NO_INDEXATION = Decimal('1.000')


class IndexationDataUnavailable(Exception):
    """A CPI quarter needed for indexation is missing. Reports mark the row pending."""


@overload
def quarter_start(day: date) -> date: ...
@overload
def quarter_start(day: None) -> None: ...
def quarter_start(day: date | None) -> date | None:
    """The first day of the CPI quarter containing `day`."""
    if day is None:
        return None
    return day.replace(month=((day.month - 1) // 3) * 3 + 1, day=1)


def index_number(day: date) -> Decimal:
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


def is_indexation_eligible(account: 'Account | None', acquisition_date: date | None, event_date: date | None,
                           declared: residency.Periods | None = None) -> bool:
    """The s114-25 test: resident every day from the later of 1 July 2027 and acquisition
    to the event.

    Any foreign, temporary or undeclared day denies it outright. False for events before
    the cutover, with no residency declared, or for a company or super fund.
    """
    if event_date is None or event_date < CGT_CUTOVER_DATE:
        return False

    if discount.taxpayer_type_of(account) in (TaxpayerType.COMPANY, TaxpayerType.SMSF):
        # s110-36(1A) indexes only for Australian resident individuals and trusts. A company
        # never had the discount it replaces, and a complying super fund keeps its third
        # (s115-100(b), unamended).
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


def indexation_factor(acquisition_date: date | None, event_date: date | None, method: str | None = None) -> Decimal:
    """The cost base growth factor from acquisition (no earlier than 1 July 2027) to the event.

    CPI ratio, or a flat annual rate if `method` is FLAT_RATE. Rounded to three places and
    never below 1.
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


def indexed_cost_base(cost_base: Any, acquisition_date: date | None, event_date: date | None,
                      method: str | None = None) -> Any:
    """`cost_base` times the indexation factor.

    Indexes the whole cost base from one date; s960-275 indexes each element from when it
    was incurred, which is immaterial for share parcels.
    """
    if cost_base is None:
        return cost_base
    factor = indexation_factor(acquisition_date, event_date, method=method)
    return cost_base * factor
