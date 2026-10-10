"""How a sale chooses its parcels, and how an adjustment is spread over them. Pure: no ORM.

The signals use these when a record is entered, and `replay` uses them to work the holdings out
again from the facts, so the two cannot drift apart.
"""

from collections.abc import Callable, Iterable, Sequence
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, TypeVar

from share_dinkum_app.choices import SellStrategy

T = TypeVar('T')


# --- sales ---------------------------------------------------------------------------------

def order_for_sale(strategy: str, candidates: Sequence[T], *, buy_date: Callable[[T], date],
                   tie: Callable[[T], Any], net_gain_per_unit: Callable[[T], Any] | None = None) -> list[T]:
    """`candidates` in the order `strategy` takes them. Ties go by `tie`, the order they were made.

    FIFO takes the earliest bought, LIFO the latest, and MIN_CGT the smallest gain per unit after
    the discount, which `net_gain_per_unit` gives.
    """
    by_tie = sorted(candidates, key=tie)
    if strategy == SellStrategy.FIFO:
        return sorted(by_tie, key=buy_date)
    if strategy == SellStrategy.LIFO:
        return sorted(by_tie, key=buy_date, reverse=True)  # A stable sort keeps ties in order.
    if strategy == SellStrategy.MIN_CGT:
        if net_gain_per_unit is None:
            raise ValueError('MIN_CGT needs the gain per unit of each parcel.')
        return sorted(by_tie, key=net_gain_per_unit)
    raise ValueError(f'No parcels are chosen automatically for a {strategy} sale.')


def take(quantity: Decimal, available: Iterable[tuple[T, Decimal]]) -> tuple[list[tuple[T, Decimal]], Decimal]:
    """Take `quantity` from each `(item, units available)` in turn.

    Returns what was taken from each, and the units left over with nowhere to go.
    """
    taken: list[tuple[T, Decimal]] = []
    remaining = quantity
    for item, units in available:
        part = min(units, remaining)
        taken.append((item, part))
        remaining -= part
        if remaining <= 0:
            break
    return taken, remaining


# --- cost base adjustments -----------------------------------------------------------------

def fiscal_year_start(end: date, start_month: int | None = None, start_day: int | None = None) -> date:
    """The first day of the fiscal year that ends on or contains `end`.

    Without a fiscal year type, the day after `end` a year earlier.
    """
    if start_month is not None and start_day is not None:
        start_this_year = date(end.year, start_month, start_day)
        if end >= start_this_year:
            return start_this_year
        return date(end.year - 1, start_month, start_day)
    try:
        return date(end.year - 1, end.month, end.day) + timedelta(days=1)
    except ValueError:
        return date(end.year - 1, end.month, 28) + timedelta(days=1)


def days_held_in_year(buy_date: date, sale_date: date | None, year_start: date, year_end: date) -> int:
    """Days a parcel was held within the year, inclusive."""
    start = max(year_start, buy_date)
    finish = min(year_end, sale_date) if sale_date else year_end
    return max((finish - start).days + 1, 0)


def units_at_year_end(quantity: Decimal, multiplier: Decimal, buy_date: date, year_end: date,
                      splits: Iterable[tuple[date, Decimal]]) -> Decimal:
    """A parcel's quantity in units as they stood at the end of the year.

    Parcels are split when a split happens, so one sold before it is still in the old units and
    one entered after a later split is in the new ones. Weighted by their own quantities, a unit
    sold before a 2-for-1 split counted for half as much as a unit still held. So each is taken
    back to the units it was bought in, then forward by the splits (date, ratio) up to the year end.
    """
    bought_units = quantity / multiplier
    for split_date, ratio in splits:
        if buy_date < split_date <= year_end:
            bought_units *= ratio
    return bought_units


def holding_weight(quantity: Decimal, multiplier: Decimal, buy_date: date, sale_date: date | None,
                   year_start: date, year_end: date, splits: Iterable[tuple[date, Decimal]]) -> Decimal:
    """Units at year end times days held in the year: a parcel's share of a QTY_HELD adjustment."""
    return (units_at_year_end(quantity, multiplier, buy_date, year_end, splits)
            * days_held_in_year(buy_date, sale_date, year_start, year_end))


def spread(total: Decimal, weighted: Iterable[tuple[T, Decimal]],
           quantize: Callable[[Decimal], Decimal]) -> list[tuple[T, Decimal, Decimal | None]]:
    """Divide `total` by weight. Returns `(item, amount, fraction)`; empty if no weight.

    Each part is rounded by `quantize` to what is stored, and the largest weight, last, takes the
    residual with fraction None, so the parts sum to the whole exactly. Without that they fall a
    few hundredths of a cent short each time, and a holding quietly loses cost base.
    """
    weighted = list(weighted)
    total_weight: Decimal | int = 0
    for _, weight in weighted:
        total_weight += weight
    if not total_weight:
        return []

    ordered = sorted(weighted, key=lambda pair: pair[1])
    parts: list[tuple[T, Decimal, Decimal | None]] = []
    allocated = Decimal('0')
    for index, (item, weight) in enumerate(ordered):
        if index == len(ordered) - 1:
            parts.append((item, total - allocated, None))
        else:
            fraction = weight / total_weight
            amount = quantize(total * fraction)
            allocated += amount
            parts.append((item, amount, fraction))
    return parts
