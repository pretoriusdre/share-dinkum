"""A holding as rows: what is stored, or what a replay works out, in one shape.

A parcel is named across the two by its lineage: its buy, then for each step down the parcel
tree the date it was made and its place among the parcels made from the same parent that day.
A share split makes one (0); a sale makes the part sold (0) and the remainder (1). So the same
history gives the same names whichever order it was worked out in, and a rebuild can update a
stored parcel in place rather than replace it.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from share_dinkum_app.models import Account

#: (buy id, (date made, place), (date made, place), ...)
Lineage = tuple[Any, ...]

#: Adjustment amounts closer than this are the same figure, rounded differently.
TOLERANCE = Decimal('0.01')


@dataclass(frozen=True)
class ParcelRow:
    ref: Any
    buy_id: Any
    parent_ref: Any
    activation: date | None
    deactivation: date | None
    quantity: Decimal
    multiplier: Decimal
    sale_date: date | None


@dataclass(frozen=True)
class SaleRow:
    sale_id: Any
    parcel_ref: Any
    quantity: Decimal
    #: Chosen by the sale's strategy in the replay, rather than taken from what is stored.
    chosen: bool = False
    #: The stored row's id; None in a replay.
    ref: Any = None


@dataclass(frozen=True)
class AdjustmentRow:
    adjustment_id: Any
    parcel_ref: Any
    amount: Decimal
    active: bool
    activation: date | None = None
    deactivation: date | None = None
    #: The stored row's id; None in a replay.
    ref: Any = None


@dataclass
class Holding:
    parcels: dict[Any, ParcelRow] = field(default_factory=dict)
    sales: list[SaleRow] = field(default_factory=list)
    adjustments: list[AdjustmentRow] = field(default_factory=list)

    def lineages(self) -> dict[Any, Lineage]:
        """Each parcel's lineage, by ref. Siblings made on one date are placed by ref, the order
        they were made in."""
        children: dict[Any, list[ParcelRow]] = defaultdict(list)
        for parcel in self.parcels.values():
            parent = parcel.parent_ref if parcel.parent_ref in self.parcels else None
            children[parent].append(parcel)

        result: dict[Any, Lineage] = {}
        roots: dict[Any, int] = defaultdict(int)
        stack: list[tuple[ParcelRow, Lineage]] = []
        for root in sorted(children[None], key=lambda p: p.ref):
            # One root per buy; a second (left by a deleted parent) is numbered after it.
            number = roots[root.buy_id]
            roots[root.buy_id] += 1
            stack.append((root, (root.buy_id,) if number == 0 else (root.buy_id, ('root', number))))
        while stack:
            parcel, lineage = stack.pop()
            result[parcel.ref] = lineage
            places: dict[date | None, int] = defaultdict(int)
            for child in sorted(children[parcel.ref], key=lambda p: p.ref):
                place = places[child.activation]
                places[child.activation] += 1
                stack.append((child, lineage + ((child.activation, place),)))
        return result


def stored(account: 'Account') -> Holding:
    """The holding as stored."""
    from share_dinkum_app.models import CostBaseAdjustmentAllocation, Parcel, SellAllocation

    holding = Holding()
    for row in Parcel.objects.filter(account=account).values(
            'id', 'buy_id', 'parent_parcel_id', 'activation_date', 'deactivation_date', 'parcel_quantity',
            'cumulative_split_multiplier', 'sale_date'):
        holding.parcels[row['id']] = ParcelRow(
            row['id'], row['buy_id'], row['parent_parcel_id'], row['activation_date'], row['deactivation_date'],
            row['parcel_quantity'], row['cumulative_split_multiplier'], row['sale_date'])
    holding.sales = [SaleRow(row['sell_id'], row['parcel_id'], row['quantity'], ref=row['id'])
                     for row in SellAllocation.objects.filter(account=account, is_active=True).values(
                         'id', 'sell_id', 'parcel_id', 'quantity')]
    holding.adjustments = [
        AdjustmentRow(row['cost_base_adjustment_id'], row['parcel_id'], row['cost_base_increase'],
                      row['deactivation_date'] is None, row['activation_date'], row['deactivation_date'], row['id'])
        for row in CostBaseAdjustmentAllocation.objects.filter(account=account).values(
            'id', 'cost_base_adjustment_id', 'parcel_id', 'cost_base_increase', 'deactivation_date', 'activation_date')]
    return holding
