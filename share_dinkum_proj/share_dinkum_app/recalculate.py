"""Recalculate the figures stored beside records, from their live properties.

Every `calculated_*` field is a copy of a property, refreshed when its own record is saved.
Records derived from others (a parcel from its buy, an allocation from its parcel) are not
saved when what they derive from changes, so their copies can fall behind. Reports read the
live properties and are unaffected; the admin lists and exports read the copies.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, Buy, CostBaseAdjustmentAllocation, ExchangeRate, Parcel, Sell

logger = logging.getLogger(__name__)


def parcels(parcels: 'Iterable[Parcel]') -> int:
    """Save each parcel, then the allocations that sold it (their gain uses its cost base)."""
    count = 0
    for parcel in parcels:
        parcel.save()
        count += 1
        for allocation in parcel.sale_allocation.filter(is_active=True):
            allocation.save()
            count += 1
    return count


def trades(buys: 'Iterable[Buy]' = (), sells: 'Iterable[Sell]' = ()) -> int:
    """Save each trade and the parcels and allocations derived from it."""
    from share_dinkum_app.models import Parcel

    count = 0
    for buy in buys:
        buy.save()
        count += 1 + parcels(Parcel.objects.filter(buy=buy, deactivation_date__isnull=True))
    for sell in sells:
        sell.save()
        count += 1
        for allocation in sell.sale_allocation.filter(is_active=True):
            allocation.save()
            count += 1
    return count


def derived_from(trade: 'Buy | Sell') -> int:
    """The parcels and allocations derived from `trade`, after its price was corrected."""
    from share_dinkum_app.models import Buy, Parcel

    if isinstance(trade, Buy):
        return parcels(Parcel.objects.filter(buy=trade, deactivation_date__isnull=True))
    count = 0
    for allocation in trade.sale_allocation.filter(is_active=True):
        allocation.save()
        count += 1
    return count


def after_rate_change(rate: 'ExchangeRate') -> int:
    """Everything converted at `rate`, after its multiplier changed."""
    from share_dinkum_app.models import Buy, Sell

    # Not just rate.buy and rate.sell: a trade with brokerage in a second currency is
    # converted at that currency's rate without being linked to it.
    count = trades(buys=Buy.converted_at(rate), sells=Sell.converted_at(rate))
    for record in [*rate.cost_base_adjustment.all(), *rate.dividend.all(),
                   *rate.distribution.all()]:
        record.save()
        count += 1
    return count


def reattach_adjustments(account: 'Account') -> int:
    """Carry adjustments left on parcels a share split replaced to what replaced them.

    Returns the number of allocations carried. See `data_checks.orphaned_adjustment_allocations`.
    """
    from share_dinkum_app import data_checks

    orphans = list(data_checks.orphaned_adjustment_allocations(account).select_related('parcel'))
    for allocation in orphans:
        _push_down(allocation)
    return len(orphans)


def _push_down(allocation: 'CostBaseAdjustmentAllocation') -> None:
    """Follow the allocation's parcel to the active parcels descended from it.

    One child is a split, so the allocation moves whole. Two are a partial sale, so it is
    divided by quantity, as the sale would have divided it.
    """
    from share_dinkum_app.models import Parcel

    parcel = allocation.parcel
    if parcel.deactivation_date is None:
        return
    children = list(Parcel.objects.filter(parent_parcel=parcel).order_by('id'))
    if len(children) == 1:
        _push_down(allocation.move_to(children[0], date=parcel.deactivation_date))
    elif len(children) == 2:
        for part in allocation.bifurcate(
                target_parcel=children[0], remainder_parcel=children[1],
                date=parcel.deactivation_date):
            _push_down(part)
    else:
        logger.warning(
            'Cost base adjustment allocation %s is on parcel %s, which was replaced by %s '
            'parcels, so where it belongs is unclear; it was left where it is.',
            allocation.pk, parcel.pk, len(children))


def refetch_placeholder_rates(account: 'Account') -> int:
    """Fetch again every stand-in exchange rate. Returns the number now real."""
    from share_dinkum_app import data_checks
    from share_dinkum_app.models import ExchangeRate

    replaced = 0
    for rate in list(data_checks.placeholder_rates(account)):
        fetched = ExchangeRate.get_or_create(
            account=account, convert_from=rate.convert_from, convert_to=rate.convert_to,
            exchange_date=rate.date)
        if not fetched.is_placeholder:
            replaced += 1
    return replaced


def account(account: 'Account') -> int:
    """Every stored figure in `account`, dependencies first. Returns the records saved."""
    from share_dinkum_app.models import (
        Buy, CostBaseAdjustment, Distribution, Dividend, Instrument, Parcel, Sell)

    count = parcels(Parcel.objects.filter(account=account, deactivation_date__isnull=True))
    for model in (Buy, Sell, CostBaseAdjustment, Dividend, Distribution):
        for record in model.objects.filter(account=account):
            record.save()
            count += 1
    # Last, since their totals are summed from the parcels above.
    for instrument in Instrument.objects.filter(account=account):
        instrument.save()
        count += 1
    logger.info('Recalculated the stored figures of %s records in %s.', count, account)
    return count
