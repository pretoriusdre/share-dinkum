"""Subdivision 112-E: the deemed sale and reacquisition on 1 July 2027.

s112-155(2) deems an asset held across the cutover sold on 30 June 2027 and reacquired on
1 July 2027 at market value; the gain is deferred to the actual sale (s112-160).

* The deferred gain is its own s102-6 category and absorbs losses first (s102-5 Step 1(a)).
* The reacquisition resets the date for indexation (s960-275(1B)) but not for the 12-month
  rule (s114-10(9)).
* Market value or the statutory apportionment is chosen at lodgment for the year of sale
  (s103-25, s112-155(4)).

s112-155(1)(d) denies the deemed sale to anyone s115-105 applies to: a foreign or temporary
resident at any time during ownership after 8 May 2012, including returned expatriates.
"""

from datetime import timedelta
from decimal import Decimal

from djmoney.money import Money

from share_dinkum_app.choices import TaxpayerType, ValuationPurpose, ValuationSource
from share_dinkum_app.cgt import discount as discount_module, residency
from share_dinkum_app.constants import CGT_CUTOVER_DATE

#: s112-155(2): deemed sale on 30 June 2027, reacquisition on 1 July 2027, at one valuation.
DEEMED_SALE_DATE = CGT_CUTOVER_DATE - timedelta(days=1)
DEEMED_REACQUISITION_DATE = CGT_CUTOVER_DATE

PURPOSE_CUTOVER = ValuationPurpose.CUTOVER_2027
PURPOSE_DEPARTURE = ValuationPurpose.DEPARTURE
PURPOSE_ARRIVAL = ValuationPurpose.ARRIVAL

#: Reasons a straddling disposal was not split.
PENDING_NO_VALUATION = (
    'No market value recorded for {instrument} on {day}, so the gain cannot be split across '
    'the cutover. Record one, or run "manage.py capture_cutover_valuations".'
)
PENDING_UNDECLARED_RESIDENCY = (
    'Residency has not been declared, and s112-155(1)(d) turns on it. Until it is, this is '
    'reported under the pre-cutover rules.'
)
PENDING_S115_105 = (
    'No deemed sale on 1 July 2027: s112-155(1)(d) denies it because s115-105 applies to '
    'this holder, who was a foreign or temporary resident at some point after 8 May 2012. '
    'The whole gain stays a single gain, with an apportioned discount and no indexation.'
)
#: The same refusal, for a holder resident every day since 1 July 2027 (s114-25), whose gain
#: is therefore indexed, and loses the discount entirely (s115-20).
PENDING_S115_105_INDEXED = (
    'No deemed sale on 1 July 2027: s112-155(1)(d) denies it because s115-105 applies to '
    'this holder, who was a foreign or temporary resident at some point after 8 May 2012. '
    'Resident every day since 1 July 2027, so the whole cost base is indexed from then '
    '(s114-25), and a gain on an indexed cost base gets no discount at all (s115-20), '
    'including on the growth before 2027.'
)


def deemed_sale_applies(account, acquisition_date, event_date, declared=None):
    """Return `(applies, reason)`: whether s112-155 splits this disposal.

    Not for a holding wholly on one side of the cutover, a company or super fund, or
    anyone abroad after 8 May 2012 during ownership. `reason` explains a refusal where
    one needs explaining, otherwise None.
    """
    if acquisition_date is None or event_date is None:
        return False, None
    if acquisition_date >= CGT_CUTOVER_DATE or event_date < CGT_CUTOVER_DATE:
        # Wholly on one side of the cutover, so there is nothing to split.
        return False, None

    if declared is None:
        declared = residency.periods(account)
    if not declared:
        return False, PENDING_UNDECLARED_RESIDENCY

    if discount_module.taxpayer_type_of(account) in (
            TaxpayerType.COMPANY, TaxpayerType.SMSF):
        # s112-155 is headed "Australian resident individuals" and works by preserving a
        # discount entitlement. An entity that never had the 50% discount has nothing to
        # preserve.
        return False, None

    if residency.has_non_resident_days_after_cutoff(
            account, acquisition_date, event_date, declared=declared):
        return False, PENDING_S115_105

    return True, None


def deemed_reset_dates(account, declared=None):
    """Dates this account's holdings are deemed sold and reacquired, as `(date, purpose)`,
    oldest first.

    * 1 July 2027 (s112-155).
    * Each departure without an s104-165(2) election (s104-165).
    * Each return to Australian residency (s855-45).
    """
    if declared is None:
        declared = residency.periods(account)

    dates = [(CGT_CUTOVER_DATE, PURPOSE_CUTOVER)]

    previous = None
    for period in declared:
        if previous is not None:
            leaving = previous.status == residency.RESIDENT and period.status != residency.RESIDENT
            arriving = previous.status != residency.RESIDENT and period.status == residency.RESIDENT
            if leaving and not period.i1_election_made:
                # The choice was not made, so CGT event I1 happened and was taxable then.
                dates.append((period.start_date, PURPOSE_DEPARTURE))
            elif arriving:
                dates.append((period.start_date, PURPOSE_ARRIVAL))
        previous = period

    return sorted(set(dates))


def unit_value_at(instrument, day, purpose=PURPOSE_CUTOVER, prefer_recorded=True):
    """Return `(unit value, source)` on `day`, or `(None, None)`.

    A recorded valuation (for `purpose` first, then any) beats that day's closing price.
    `prefer_recorded=False` uses the closing price only, for callers replacing a valuation.
    """
    from share_dinkum_app.models import InstrumentPriceHistory, InstrumentValuation

    if prefer_recorded:
        recorded = InstrumentValuation.objects.filter(
            instrument=instrument, valuation_date=day, is_active=True)
        # A valuation taken for this purpose first, then one taken for any other: a value
        # sourced for a departure is still the best answer for what a unit was worth that
        # day, even if it was not recorded with the cutover in mind.
        valuation = (recorded.filter(purpose=purpose).first() or recorded.first())
        if valuation is not None:
            return valuation.unit_value, valuation.source

    close = (
        InstrumentPriceHistory.objects
        .filter(instrument=instrument, date=day)
        .first()
    )
    if close is not None and close.close is not None:
        # Price history is a bare decimal quoted in the instrument's own currency, unlike a
        # recorded valuation which carries its currency with it.
        return Money(close.close, instrument.currency), ValuationSource.PRICE_HISTORY

    return None, None


def scale_for_splits(parcel, day):
    """The combined ratio of the instrument's share splits after `day`.

    Divide a per-unit value from `day` by this to get a value per current unit.
    """
    from share_dinkum_app.models import ShareSplit

    multiplier = Decimal('1')
    splits = ShareSplit.objects.filter(
        account=parcel.account, instrument=parcel.buy.instrument,
        date__gt=day, is_active=True)
    if parcel.sale_date is not None:
        # A split after the sale never reached the parcel, so its units are still as sold.
        splits = splits.filter(date__lte=parcel.sale_date)
    for split in splits:
        if split.quantity_before:
            multiplier *= Decimal(split.quantity_after) / Decimal(split.quantity_before)
    return multiplier
