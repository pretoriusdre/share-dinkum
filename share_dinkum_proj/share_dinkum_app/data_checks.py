"""Checks for records left wrong by bugs since fixed, or by entries the app cannot resolve.

Each check is one query, so the dashboard runs them all on every visit. The
`repair_portfolio_data` command fixes those marked `repairable` and lists the rest, which
need a person: only they know which parcel a sale was of, or what an adjustment should be.
"""

from dataclasses import dataclass
from decimal import Decimal

from django.db.models import F, Q, Sum
from django.db.models.functions import Abs, Coalesce

COMMAND = 'uv run dev repair_portfolio_data'


@dataclass
class Finding:
    key: str
    count: int
    summary: str
    #: Fixed by `repair_portfolio_data`, rather than needing a person.
    repairable: bool
    #: Capital gains are wrong until it is dealt with, not just a stored copy.
    affects_gains: bool


def unallocated_sales(account):
    from share_dinkum_app.models import Sell
    return Sell.with_unallocated_quantity(account)


def _parcels_with_sold(account):
    from share_dinkum_app.models import Parcel
    return Parcel.objects.filter(account=account, deactivation_date__isnull=True).annotate(
        sold=Coalesce(
            Sum('sale_allocation__quantity', filter=Q(sale_allocation__is_active=True)),
            Decimal('0')))


def oversold_parcels(account):
    """Parcels allocated to sales for more units than they hold."""
    return _parcels_with_sold(account).filter(sold__gt=F('parcel_quantity')).select_related(
        'buy__instrument')


def stale_sold_parcels(account):
    """Parcels whose stored sold flag disagrees with their allocations."""
    return _parcels_with_sold(account).filter(
        Q(calculated_is_sold__isnull=True)
        | Q(calculated_is_sold=False, sold__gte=F('parcel_quantity'))
        | Q(calculated_is_sold=True, sold__lt=F('parcel_quantity')))


def orphaned_adjustment_allocations(account):
    """Adjustments still attached to a parcel a share split replaced, so in no cost base."""
    from share_dinkum_app.models import CostBaseAdjustmentAllocation
    return CostBaseAdjustmentAllocation.objects.filter(
        account=account, is_active=True, parcel__deactivation_date__isnull=False)


def unbalanced_adjustments(account):
    """Adjustments whose allocations do not add up to them, e.g. spread at a stand-in rate."""
    from share_dinkum_app.models import CostBaseAdjustment
    return (
        CostBaseAdjustment.objects
        .filter(account=account, allocation_method='QTY_HELD',
                cost_base_adjustment_allocation__isnull=False)
        .annotate(allocated=Coalesce(
            Sum('cost_base_adjustment_allocation__cost_base_increase',
                filter=Q(cost_base_adjustment_allocation__is_active=True)),
            Decimal('0')))
        .filter(calculated_cost_base_increase_converted__isnull=False)
        .annotate(difference=Abs(F('allocated') - F('calculated_cost_base_increase_converted')))
        .filter(difference__gt=Decimal('0.0001'))
        .distinct()
    )


def placeholder_rates(account):
    from share_dinkum_app.models import ExchangeRate
    return ExchangeRate.objects.filter(account=account, is_placeholder=True)


def run(account):
    """Every check with something to report, those affecting gains first."""
    from share_dinkum_app.models import CostBaseAdjustment, Parcel

    checks = [
        ('unallocated_sales', unallocated_sales(account).count(),
         'sale(s) with units allocated to no parcel, whose gain is in no report',
         False, True),
        ('oversold_parcels', oversold_parcels(account).count(),
         'parcel(s) allocated to sales for more units than they hold',
         False, True),
        ('unconverted_adjustments', CostBaseAdjustment.with_unconverted_allocations(account).count(),
         'cost base adjustment(s) allocated without being converted to '
         f'{account.currency}, which must be deleted and entered again',
         False, True),
        ('unbalanced_adjustments', unbalanced_adjustments(account).count(),
         'cost base adjustment(s) whose allocations do not add up to them, which must be '
         'deleted and entered again',
         False, True),
        ('orphaned_adjustment_allocations', orphaned_adjustment_allocations(account).count(),
         'cost base adjustment allocation(s) left behind by a share split, and so missing '
         'from the cost base',
         True, True),
        ('placeholder_rates', placeholder_rates(account).count(),
         'exchange rate(s) standing in for one that could not be fetched',
         True, True),
        ('unconverted_parcels', Parcel.with_unconverted_cost_base(account).count(),
         f'parcel(s) with their cost base stored in a currency other than {account.currency}',
         True, False),
        ('stale_sold_parcels', stale_sold_parcels(account).count(),
         'parcel(s) whose stored sold flag is out of date',
         True, False),
    ]
    findings = [
        Finding(key, count, f'{count} {summary}', repairable, affects_gains)
        for key, count, summary, repairable, affects_gains in checks if count
    ]
    return sorted(findings, key=lambda finding: not finding.affects_gains)
