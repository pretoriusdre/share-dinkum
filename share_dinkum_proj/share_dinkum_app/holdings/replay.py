"""Work a holding out again from its facts, in date order, without touching the database.

On one date: splits, then buys, then sales, then adjustments. A split skips that day's buys, which
are already in post-split units, and a sale can use them. Ties go by id, the order the records
were made.

Decisions already made are applied as they were (see `facts`). Only a sale with no allocations
chooses its parcels, and it leaves alone the units a later pinned sale took.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from share_dinkum_app.choices import AllocationMethod, SellStrategy
from share_dinkum_app.holdings import strategies
from share_dinkum_app.holdings.facts import AdjustmentFact, BuyFact, Facts, SaleFact, SplitFact

#: The precision each figure is stored at, so the replay rounds where the database does.
QUANTITY_PLACES = Decimal('0.0001')
MULTIPLIER_PLACES = Decimal('0.0000000001')
AMOUNT_PLACES = Decimal('0.0001')

SPLIT, BUY, SALE, ADJUSTMENT = range(4)


def _round(value: Decimal, places: Decimal) -> Decimal:
    return value.quantize(places, rounding=ROUND_HALF_UP)


def _amount(value: Decimal) -> Decimal:
    return _round(value, AMOUNT_PLACES)


# --- the result, in the shape `compare` reads the stored holding in too ----------------------

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


@dataclass(frozen=True)
class AdjustmentRow:
    adjustment_id: Any
    parcel_ref: Any
    amount: Decimal
    active: bool


@dataclass
class Holding:
    parcels: dict[Any, ParcelRow] = field(default_factory=dict)
    sales: list[SaleRow] = field(default_factory=list)
    adjustments: list[AdjustmentRow] = field(default_factory=list)


@dataclass
class Replayed:
    holding: Holding
    #: Units of each sale with no parcel to come from.
    unallocated: dict[Any, Decimal] = field(default_factory=dict)
    #: Decisions that could not be applied as stored, e.g. a sale of more units than the buy held.
    problems: list[str] = field(default_factory=list)
    #: (adjustment, buy, pinned amount, amount a fresh spread gives) where they differ by a cent or more.
    respread: list[tuple[AdjustmentFact, BuyFact, Decimal, Decimal]] = field(default_factory=list)


# --- working state -------------------------------------------------------------------------

@dataclass(eq=False)
class _Parcel:
    ref: int
    buy: BuyFact
    quantity: Decimal
    multiplier: Decimal
    activation: date
    parent: '_Parcel | None' = None
    deactivation: date | None = None
    sale_date: date | None = None

    @property
    def active(self) -> bool:
        return self.deactivation is None

    @property
    def sold(self) -> bool:
        return self.sale_date is not None


@dataclass(eq=False)
class _Part:
    """Part of an adjustment, on one parcel."""
    adjustment_id: Any
    parcel: _Parcel
    amount: Decimal
    deactivation: date | None = None


class _Replay:

    def __init__(self, facts: Facts) -> None:
        self.facts = facts
        self.parcels: list[_Parcel] = []
        self.parts: list[_Part] = []
        self.sale_rows: list[SaleRow] = []
        self.result = Replayed(holding=Holding())
        self.buys = {buy.id: buy for buy in facts.buys}

        self.events: list[tuple[date, int, str, Any]] = sorted(
            [(split.date, SPLIT, str(split.id), split) for split in facts.splits]
            + [(buy.date, BUY, str(buy.id), buy) for buy in facts.buys]
            + [(sale.date, SALE, str(sale.id), sale) for sale in facts.sales]
            + [(adjustment.year_end, ADJUSTMENT, str(adjustment.id), adjustment)
               for adjustment in facts.adjustments],
            key=lambda event: event[:3])

        self.pins: dict[Any, list[Any]] = defaultdict(list)
        for pin in sorted(facts.pinned_sales, key=lambda pin: pin.order):
            self.pins[pin.sale_id].append(pin)
        self.claims = self._later_claims()

    # --- parcels ---

    def _new(self, buy: BuyFact, quantity: Decimal, multiplier: Decimal, activation: date,
             parent: _Parcel | None = None) -> _Parcel:
        parcel = _Parcel(len(self.parcels), buy, quantity, multiplier, activation, parent)
        self.parcels.append(parcel)
        return parcel

    def _active(self, instrument: str) -> list[_Parcel]:
        return [p for p in self.parcels if p.active and p.buy.instrument == instrument]

    def _parts_on(self, parcel: _Parcel) -> list[_Part]:
        return [part for part in self.parts if part.parcel is parcel and part.deactivation is None]

    def _bifurcate(self, parcel: _Parcel, quantity: Decimal, day: date) -> _Parcel:
        """As `Parcel.bifurcate`: split off `quantity`, carrying adjustments by quantity."""
        if quantity == parcel.quantity:
            return parcel
        target = self._new(parcel.buy, quantity, parcel.multiplier, day, parent=parcel)
        remainder = self._new(parcel.buy, parcel.quantity - quantity, parcel.multiplier, day, parent=parcel)
        parcel.deactivation = day
        fraction = target.quantity / (target.quantity + remainder.quantity)
        for part in self._parts_on(parcel):
            target_amount = _amount(part.amount * fraction)
            self.parts.append(_Part(part.adjustment_id, target, target_amount))
            self.parts.append(_Part(part.adjustment_id, remainder, part.amount - target_amount))
            part.deactivation = day
        return target

    # --- events ---

    def split(self, split: SplitFact) -> None:
        for parcel in self._active(split.instrument):
            if parcel.buy.date >= split.date or parcel.sold:
                continue
            new = self._new(
                parcel.buy, _round(parcel.quantity * split.ratio, QUANTITY_PLACES),
                _round(parcel.multiplier * split.ratio, MULTIPLIER_PLACES), split.date, parent=parcel)
            parcel.deactivation = split.date
            for part in self._parts_on(parcel):
                self.parts.append(_Part(part.adjustment_id, new, part.amount))
                part.deactivation = split.date

    def buy(self, buy: BuyFact) -> None:
        self._new(buy, buy.quantity, Decimal('1'), buy.date)

    def sale(self, sale: SaleFact, position: int) -> None:
        pins = self.pins.get(sale.id, [])
        taken = Decimal('0')
        if pins:
            for pin in pins:
                parcel = next((p for p in self._active(sale.instrument)
                               if p.buy.id == pin.buy_id and not p.sold), None)
                if parcel is None or pin.quantity > parcel.quantity:
                    held = parcel.quantity if parcel else Decimal('0')
                    self.result.problems.append(
                        f'{sale.label()} took {pin.quantity:f} units from {self.buys[pin.buy_id].label()}, '
                        f'which held {held:f} unsold on that date.')
                    continue
                self._sell(sale, parcel, pin.quantity, chosen=False)
                taken += pin.quantity
        elif sale.strategy != SellStrategy.MANUAL:
            taken = self._choose(sale, position)

        if sale.quantity - taken > 0:
            self.result.unallocated[sale.id] = sale.quantity - taken

    def _sell(self, sale: SaleFact, parcel: _Parcel, quantity: Decimal, chosen: bool) -> None:
        sold = self._bifurcate(parcel, quantity, sale.date)
        sold.sale_date = sale.date
        self.sale_rows.append(SaleRow(sale.id, sold.ref, quantity, chosen))

    def _choose(self, sale: SaleFact, position: int) -> Decimal:
        """Allocate a sale stored with no allocations, by its strategy."""
        candidates = [p for p in self._active(sale.instrument) if p.buy.date <= sale.date]

        def available(parcel: _Parcel) -> Decimal:
            if parcel.sold:
                return Decimal('0')
            reserved = sum((units for later, units in self.claims[parcel.buy.id] if later > position), Decimal('0'))
            return parcel.quantity - reserved * parcel.multiplier

        net_gain = None
        if sale.strategy == SellStrategy.MIN_CGT and self.facts.net_gain_per_unit is not None:
            rank = self.facts.net_gain_per_unit
            net_gain = lambda parcel: rank(sale, parcel.buy, self._unit_cost_base(parcel))  # noqa: E731

        ordered = strategies.order_for_sale(
            sale.strategy, candidates, buy_date=lambda p: p.buy.date, tie=lambda p: p.ref,
            net_gain_per_unit=net_gain)
        taken, _ = strategies.take(sale.quantity, (
            (parcel, available(parcel)) for parcel in ordered if available(parcel) > 0))
        for parcel, quantity in taken:
            self._sell(sale, parcel, quantity, chosen=True)
        return sum((quantity for _, quantity in taken), Decimal('0'))

    def _unit_cost_base(self, parcel: _Parcel) -> Decimal:
        price = (parcel.buy.unit_price or Decimal('0')) / parcel.multiplier
        brokerage = (parcel.buy.unit_brokerage or Decimal('0')) / parcel.multiplier
        adjustments = sum((part.amount for part in self._parts_on(parcel)), Decimal('0'))
        return (price * parcel.quantity + brokerage * parcel.quantity + adjustments) / parcel.quantity

    def _later_claims(self) -> dict[Any, list[tuple[int, Decimal]]]:
        """Per buy, (event position, units in bought units) of every pinned sale."""
        positions = {event[3].id: index for index, event in enumerate(self.events) if event[1] == SALE}
        sales = {sale.id: sale for sale in self.facts.sales}
        claims: dict[Any, list[tuple[int, Decimal]]] = defaultdict(list)
        for pin in self.facts.pinned_sales:
            sale, buy = sales.get(pin.sale_id), self.buys.get(pin.buy_id)
            if sale is None or buy is None:
                continue
            multiplier = Decimal('1')
            for split in self.facts.splits:
                if split.instrument == buy.instrument and buy.date < split.date <= sale.date:
                    multiplier *= split.ratio
            claims[buy.id].append((positions[sale.id], pin.quantity / multiplier))
        return claims

    def adjustment(self, adjustment: AdjustmentFact) -> None:
        start, end = adjustment.year_start, adjustment.year_end
        splits = [(s.date, s.ratio) for s in self.facts.splits
                  if s.instrument == adjustment.instrument and s.is_active]
        eligible = [p for p in self._active(adjustment.instrument)
                    if p.buy.date <= end and (p.sale_date is None or p.sale_date >= start)]

        def weight(parcel: _Parcel) -> Decimal:
            return strategies.holding_weight(
                parcel.quantity, parcel.multiplier, parcel.buy.date, parcel.sale_date, start, end, splits)

        qty_held = adjustment.method == AllocationMethod.QTY_HELD
        fresh = strategies.spread(adjustment.amount, [(p, weight(p)) for p in eligible], _amount) if qty_held else []

        if not adjustment.spread_at_entry:
            for parcel, amount, _ in fresh:
                self.parts.append(_Part(adjustment.id, parcel, amount))
            return

        pinned = {buy_id: amount for (adjustment_id, buy_id), amount in self.facts.pinned_spreads.items()
                  if adjustment_id == adjustment.id}
        for buy_id, amount in sorted(pinned.items(), key=lambda item: str(item[0])):
            if qty_held:
                parts = strategies.spread(amount, [(p, weight(p)) for p in eligible if p.buy.id == buy_id], _amount)
            else:
                # Entered by hand: spread over what the buy held, by units.
                parts = strategies.spread(amount, [(p, p.quantity) for p in self._active(adjustment.instrument)
                                                   if p.buy.id == buy_id], _amount)
            if not parts:
                self.result.problems.append(
                    f'{adjustment.label()} gave {amount:f} to {self.buys[buy_id].label()}, which held '
                    f'nothing in that year.')
                continue
            for parcel, part_amount, _ in parts:
                self.parts.append(_Part(adjustment.id, parcel, part_amount))

        if qty_held:
            fresh_by_buy: dict[Any, Decimal] = defaultdict(Decimal)
            for parcel, amount, _ in fresh:
                fresh_by_buy[parcel.buy.id] += amount
            for buy_id in sorted(set(pinned) | set(fresh_by_buy), key=str):
                stored, worked = pinned.get(buy_id, Decimal('0')), fresh_by_buy.get(buy_id, Decimal('0'))
                if abs(stored - worked) >= Decimal('0.01'):
                    self.result.respread.append((adjustment, self.buys[buy_id], stored, worked))

    def run(self) -> Replayed:
        for position, (_, kind, _, fact) in enumerate(self.events):
            if kind == SPLIT:
                self.split(fact)
            elif kind == BUY:
                self.buy(fact)
            elif kind == SALE:
                self.sale(fact, position)
            else:
                self.adjustment(fact)

        holding = self.result.holding
        for p in self.parcels:
            holding.parcels[p.ref] = ParcelRow(
                p.ref, p.buy.id, p.parent.ref if p.parent else None, p.activation, p.deactivation,
                p.quantity, p.multiplier, p.sale_date)
        holding.sales = list(self.sale_rows)
        holding.adjustments = [AdjustmentRow(part.adjustment_id, part.parcel.ref, part.amount,
                                             part.deactivation is None) for part in self.parts]
        return self.result


def replay(facts: Facts) -> Replayed:
    """The holding the facts give, worked out from nothing in date order."""
    return _Replay(facts).run()
