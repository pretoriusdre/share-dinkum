"""Subdivision 112-E: the deemed sale and reacquisition on 1 July 2027.

The Act does not split a straddling gain arithmetically. It deems the asset **sold on
30 June 2027 and reacquired on 1 July 2027** at market value (s112-155(2)), and defers the
resulting gain or loss until the asset is actually disposed of (s112-160). One economic
disposal therefore produces two gains with different characters: the deferred one, which
keeps the 50% discount, and the gain on growth after the cutover, which is indexed instead.

Three consequences the arithmetic-split model does not capture, and which this module
exists to get right:

* The deferred gain is a **separate category** under s102-6, not a component of the later
  gain. It absorbs capital losses first, ahead of everything else (s102-5 Step 1(a)).
* The reacquisition resets the acquisition date for indexation (s960-275(1B)) but is
  **disregarded for the 12-month rule** (s114-10(9)), so a parcel bought in 2020 and sold in
  2028 counts as held for more than twelve months on both sides of the cutover.
* Nothing has to be decided in June 2027. The choice between a market valuation and the
  statutory apportionment is made when the return for the year of the actual sale is lodged
  (s103-25, s112-155(4)), so a user who does nothing at the cutover has lost no option.

**s112-155(1)(d) denies all of this to anyone s115-105 applies to**, which reaches much
further than "foreign residents". s115-105(2)(e) catches anyone who was a foreign *or
temporary* resident during any part of the ownership period after 8 May 2012 -- a returned
expatriate, or a former 482 visa holder who has since become a permanent resident. They get
no deemed sale, so no 50% is banked on their pre-2027 growth, and if they are resident from
the cutover then indexation applies and s115-20 takes the discount away as well. They can
end up with neither. See `pending_reason` on the events this produces: the report has to say
that out loud, because it looks like a bug.
"""

from datetime import timedelta
from decimal import Decimal

from djmoney.money import Money

from share_dinkum_app.choices import TaxpayerType, ValuationPurpose, ValuationSource
from share_dinkum_app.cgt import discount as discount_module, residency
from share_dinkum_app.constants import CGT_CUTOVER_DATE

#: s112-155(2) deems the sale to happen just before 1 July 2027 and the reacquisition on it.
#: The valuation is the same figure for both, so one date is enough to look one up, but the
#: gain either side of it belongs to different regimes and the two names keep that legible.
DEEMED_SALE_DATE = CGT_CUTOVER_DATE - timedelta(days=1)
DEEMED_REACQUISITION_DATE = CGT_CUTOVER_DATE

PURPOSE_CUTOVER = ValuationPurpose.CUTOVER_2027
PURPOSE_DEPARTURE = ValuationPurpose.DEPARTURE
PURPOSE_ARRIVAL = ValuationPurpose.ARRIVAL

#: Why a straddling disposal could not be split, in the order the caller should care about.
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


def deemed_sale_applies(account, acquisition_date, event_date, declared=None):
    """Whether s112-155 splits this disposal in two.

    Returns `(applies, reason)`. The reason is None when it applies, and otherwise says
    which condition failed, because "your gain was not split" is not a self-explaining
    outcome for someone who read about the reform in the paper.
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
    """Every date on which this account's holdings are deemed to be sold and reacquired.

    One mechanism, four provisions. Each of these resets a cost base to market value on a
    particular day, and the only difference between them is what happens to the gain that
    falls out:

    * **1 July 2027** (s112-155) -- deferred until the asset is actually sold.
    * **Leaving Australia** (s104-165) -- taxable then, unless the s104-165(2) choice was
      made, in which case there is no reset at all and the assets stay in the Australian net.
    * **Becoming an Australian resident** (s855-45) -- assets that were outside the
      Australian net are brought in at their market value on that day, so growth from before
      arrival is never taxed here.

    Returns a list of `(date, purpose)` oldest first. A caller wanting a valuation asks for
    each of these; nothing here decides what the valuation is used for.
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
    """What one unit was worth on a day, and where the figure came from.

    Returns `(value, source)`, or `(None, None)`. A valuation recorded against the day is
    preferred over a closing price, because the user may have had to source one for an
    unlisted or suspended holding and their answer should not be silently overridden by a
    stale price.

    `prefer_recorded=False` skips recorded valuations and goes to the market data. It exists
    for the one caller that is deliberately replacing a valuation: without it, asking what
    the value is would return the value being replaced, and an overwrite would write back
    what was already there.
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
    """Adjust a per-unit value recorded before a share split.

    A valuation is per unit at the time it was taken. If the instrument has split since,
    today's units are not that unit. The parcel's own multiplier records the ratio.
    """
    from share_dinkum_app.models import ShareSplit

    multiplier = Decimal('1')
    splits = ShareSplit.objects.filter(
        account=parcel.account, instrument=parcel.buy.instrument,
        date__gt=day, is_active=True)
    for split in splits:
        if split.quantity_before:
            multiplier *= Decimal(split.quantity_after) / Decimal(split.quantity_before)
    return multiplier
