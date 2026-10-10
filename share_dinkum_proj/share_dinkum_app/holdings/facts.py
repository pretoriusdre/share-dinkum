"""The facts a holding is worked out from, read from the database into plain dataclasses.

Facts are what was entered: buys, sales, splits and adjustments. Decisions already made are facts
too, since working them out again could change a lodged year:

* which parcels each sale used (every active SellAllocation, as sale, buy and quantity), and
* how each adjustment already spread was divided between buys (the active allocations, summed
  per buy). Data entered before 0.4.0 may have been spread over an incomplete holding.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from django.db.models import Sum
from djmoney.money import Money

from share_dinkum_app.holdings import strategies

if TYPE_CHECKING:
    from share_dinkum_app.models import Account


@dataclass(frozen=True)
class BuyFact:
    id: Any
    instrument: str
    date: date
    quantity: Decimal
    legacy_id: str | None
    #: Price and brokerage per unit in the portfolio currency, for a MIN_CGT sale to rank by.
    unit_price: Decimal | None = None
    unit_brokerage: Decimal | None = None

    def label(self) -> str:
        reference = f' ({self.legacy_id})' if self.legacy_id else ''
        return f'{self.instrument} bought {self.date}, {self.quantity:f} units{reference}'


@dataclass(frozen=True)
class SaleFact:
    id: Any
    instrument: str
    date: date
    quantity: Decimal
    strategy: str
    legacy_id: str | None
    #: Net proceeds per unit in the portfolio currency, for MIN_CGT.
    unit_proceeds: Decimal | None = None

    def label(self) -> str:
        reference = f' ({self.legacy_id})' if self.legacy_id else ''
        return f'{self.instrument} sold {self.date}, {self.quantity:f} units{reference}'


@dataclass(frozen=True)
class PinnedSale:
    """Units a sale took from a buy, in units as they stood on the sale date."""
    sale_id: Any
    buy_id: Any
    quantity: Decimal
    #: The allocation's id, so a sale's parts are taken in the order they were made.
    order: str


@dataclass(frozen=True)
class SplitFact:
    id: Any
    instrument: str
    date: date
    ratio: Decimal
    #: Inactive splits are still applied, but not counted when weighting an adjustment.
    is_active: bool = True


@dataclass(frozen=True)
class AdjustmentFact:
    id: Any
    instrument: str
    year_start: date
    year_end: date
    method: str
    #: In the portfolio currency.
    amount: Decimal
    legacy_id: str | None = None
    #: Already spread when it was entered, so its division between buys is pinned.
    spread_at_entry: bool = True

    def label(self) -> str:
        return f'{self.instrument} adjustment for the year ending {self.year_end}'


@dataclass
class Facts:
    currency: str
    buys: list[BuyFact] = field(default_factory=list)
    sales: list[SaleFact] = field(default_factory=list)
    pinned_sales: list[PinnedSale] = field(default_factory=list)
    splits: list[SplitFact] = field(default_factory=list)
    adjustments: list[AdjustmentFact] = field(default_factory=list)
    #: (adjustment id, buy id) -> amount, for each adjustment spread when it was entered.
    pinned_spreads: dict[tuple[Any, Any], Decimal] = field(default_factory=dict)
    #: Ranks a MIN_CGT parcel: (sale, buy, unit cost base) -> net gain per unit after discount.
    net_gain_per_unit: Callable[[SaleFact, BuyFact, Decimal], Any] | None = None


def _amount(money: Money | None) -> Decimal | None:
    return None if money is None else money.amount


def load(account: 'Account') -> Facts:
    """Every fact for `account`."""
    from share_dinkum_app import cgt
    from share_dinkum_app.models import (
        Buy, CostBaseAdjustment, CostBaseAdjustmentAllocation, Sell, SellAllocation, ShareSplit,
    )

    currency = str(account.currency)
    fiscal_year_type = account.fiscal_year_type

    facts = Facts(currency=currency)
    for buy in Buy.objects.filter(account=account).select_related('instrument', 'exchange_rate'):
        facts.buys.append(BuyFact(
            id=buy.pk, instrument=buy.instrument.name, date=buy.date, quantity=buy.quantity,
            legacy_id=buy.legacy_id, unit_price=_amount(buy.unit_price_converted),
            unit_brokerage=_amount(buy.unit_brokerage_converted)))
    for sell in Sell.objects.filter(account=account).select_related('instrument', 'exchange_rate'):
        facts.sales.append(SaleFact(
            id=sell.pk, instrument=sell.instrument.name, date=sell.date, quantity=sell.quantity,
            strategy=sell.strategy, legacy_id=sell.legacy_id, unit_proceeds=_amount(sell.unit_proceeds)))
    for allocation in SellAllocation.objects.filter(account=account, is_active=True).values(
            'id', 'sell_id', 'parcel__buy_id', 'quantity'):
        facts.pinned_sales.append(PinnedSale(
            sale_id=allocation['sell_id'], buy_id=allocation['parcel__buy_id'],
            quantity=allocation['quantity'], order=str(allocation['id'])))
    # In id order, as the signals read them, so ratios multiply in the same order.
    for split in ShareSplit.objects.filter(account=account).select_related('instrument').order_by('id'):
        facts.splits.append(SplitFact(
            id=split.pk, instrument=split.instrument.name, date=split.date, ratio=split.ratio,
            is_active=split.is_active))
    for adjustment in CostBaseAdjustment.objects.filter(account=account).select_related('instrument', 'exchange_rate'):
        end = adjustment.financial_year_end_date
        facts.adjustments.append(AdjustmentFact(
            id=adjustment.pk, instrument=adjustment.instrument.name,
            year_start=strategies.fiscal_year_start(end, fiscal_year_type.start_month, fiscal_year_type.start_day),
            year_end=end, method=adjustment.allocation_method,
            amount=adjustment.cost_base_increase_converted.amount, legacy_id=adjustment.legacy_id,
            spread_at_entry=adjustment._creation_handled))
    spreads = (
        CostBaseAdjustmentAllocation.objects
        .filter(account=account, deactivation_date__isnull=True)
        .values('cost_base_adjustment_id', 'parcel__buy_id')
        .annotate(total=Sum('cost_base_increase'))
    )
    for row in spreads:
        facts.pinned_spreads[(row['cost_base_adjustment_id'], row['parcel__buy_id'])] = row['total']

    def net_gain_per_unit(sale: SaleFact, buy: BuyFact, unit_cost_base: Decimal) -> Any:
        """As the MIN_CGT signal ranks a parcel: the reports' discount on the gain per unit."""
        return cgt.apply_discount(
            Money((sale.unit_proceeds or Decimal('0')) - unit_cost_base, currency),
            purchase_date=buy.date, sale_date=sale.date, account=account)

    facts.net_gain_per_unit = net_gain_per_unit
    return facts
