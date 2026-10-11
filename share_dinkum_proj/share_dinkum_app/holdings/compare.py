"""The stored holding set against the replayed one, at the level that changes a figure.

For each sale and buy: the units sold and the adjustments they carry, which set the gain. For each
buy: the units still held, their split multiplier and their adjustments, which set the cost base.
Two holdings can agree on all of that and still differ in shape: entered in another order, a
parcel splits into different pieces. That is counted, not reported. So is an adjustment that
differs by less than a cent, which is rounding: a parcel divided at a sale carries its adjustment
by quantity, and one spread after the sale was weighted directly.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from share_dinkum_app.holdings import facts as facts_module
from share_dinkum_app.holdings.facts import Facts
from share_dinkum_app.holdings.replay import Replayed, replay
from share_dinkum_app.holdings.state import TOLERANCE, Holding, stored

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, Instrument



@dataclass(frozen=True)
class Difference:
    kind: str
    subject: str
    stored: str
    replayed: str

    def __str__(self) -> str:
        return f'{self.kind}: {self.subject}. Stored {self.stored}, replayed {self.replayed}.'


@dataclass
class Report:
    differences: list[Difference] = field(default_factory=list)
    #: Decisions the replay could not apply as stored.
    problems: list[str] = field(default_factory=list)
    #: Pinned spreads that a fresh spread would divide differently. Kept, not a difference.
    respread: list[str] = field(default_factory=list)
    #: Parcels in one holding and not the other, where no figure differs.
    shape_differences: int = 0
    #: Adjustment differences under TOLERANCE.
    rounding: int = 0
    parcels_stored: int = 0
    parcels_replayed: int = 0

    @property
    def count(self) -> int:
        """Differences and problems: what a person should look at."""
        return len(self.differences) + len(self.problems)


@dataclass
class _Summary:
    sold_units: dict[tuple[Any, Any], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    sold_adjustments: dict[tuple[Any, Any, Any], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    held_units: dict[Any, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    held_multipliers: dict[Any, set[Decimal]] = field(default_factory=lambda: defaultdict(set))
    held_adjustments: dict[tuple[Any, Any], Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    #: Adjustments on parcels neither held nor sold, e.g. left on a parcel a split replaced.
    stranded: dict[Any, Decimal] = field(default_factory=lambda: defaultdict(Decimal))
    shape: Counter[tuple[Any, ...]] = field(default_factory=Counter)


def summarise(holding: Holding) -> _Summary:
    summary = _Summary()
    sold_by: dict[Any, Any] = {}
    for row in holding.sales:
        parcel = holding.parcels[row.parcel_ref]
        summary.sold_units[(row.sale_id, parcel.buy_id)] += row.quantity
        sold_by.setdefault(row.parcel_ref, row.sale_id)

    for parcel in holding.parcels.values():
        summary.shape[(parcel.buy_id, parcel.activation, parcel.deactivation, parcel.quantity,
                       parcel.multiplier, parcel.sale_date)] += 1
        if parcel.deactivation is None and parcel.ref not in sold_by:
            summary.held_units[parcel.buy_id] += parcel.quantity
            summary.held_multipliers[parcel.buy_id].add(parcel.multiplier.normalize())

    for part in holding.adjustments:
        if not part.active:
            continue
        parcel = holding.parcels[part.parcel_ref]
        if part.parcel_ref in sold_by:
            summary.sold_adjustments[(sold_by[part.parcel_ref], parcel.buy_id, part.adjustment_id)] += part.amount
        elif parcel.deactivation is None:
            summary.held_adjustments[(parcel.buy_id, part.adjustment_id)] += part.amount
        else:
            summary.stranded[part.adjustment_id] += part.amount
    return summary


def compare(facts: Facts, stored_holding: Holding, replayed: Replayed) -> Report:
    """Where the stored holding and the replayed one give different figures."""
    buys = {buy.id: buy for buy in facts.buys}
    sales = {sale.id: sale for sale in facts.sales}
    adjustments = {adjustment.id: adjustment for adjustment in facts.adjustments}
    a, b = summarise(stored_holding), summarise(replayed.holding)
    report = Report(problems=list(replayed.problems), parcels_stored=len(stored_holding.parcels),
                    parcels_replayed=len(replayed.holding.parcels))

    def buy_label(buy_id: Any) -> str:
        return buys[buy_id].label() if buy_id in buys else 'a buy that no longer exists'

    def differ(kind: str, subject: str, stored_value: Any, replayed_value: Any) -> None:
        report.differences.append(Difference(kind, subject, _show(stored_value), _show(replayed_value)))

    def amounts(kind: str, describe: Any, stored_amounts: dict[Any, Decimal], replayed_amounts: dict[Any, Decimal]) -> None:
        for key in sorted(set(stored_amounts) | set(replayed_amounts), key=str):
            s, r = stored_amounts.get(key, Decimal('0')), replayed_amounts.get(key, Decimal('0'))
            if abs(s - r) >= TOLERANCE:
                differ(kind, describe(key), s, r)
            elif s != r:
                report.rounding += 1

    for key in sorted(set(a.sold_units) | set(b.sold_units), key=str):
        s, r = a.sold_units.get(key, Decimal('0')), b.sold_units.get(key, Decimal('0'))
        if s != r:
            sale_id, buy_id = key
            differ('Units sold', f'{sales[sale_id].label()}, from {buy_label(buy_id)}', s, r)

    amounts('Adjustment on units sold',
            lambda key: f'{adjustments[key[2]].label()}, on {sales[key[0]].label()} from {buy_label(key[1])}',
            a.sold_adjustments, b.sold_adjustments)

    for buy_id in sorted(set(a.held_units) | set(b.held_units), key=str):
        s, r = a.held_units.get(buy_id, Decimal('0')), b.held_units.get(buy_id, Decimal('0'))
        if s != r:
            differ('Units held', buy_label(buy_id), s, r)
        elif a.held_multipliers.get(buy_id) != b.held_multipliers.get(buy_id):
            differ('Split multiplier', buy_label(buy_id),
                   sorted(a.held_multipliers.get(buy_id, set())), sorted(b.held_multipliers.get(buy_id, set())))

    amounts('Adjustment on units held',
            lambda key: f'{adjustments[key[1]].label()}, on {buy_label(key[0])}',
            a.held_adjustments, b.held_adjustments)
    amounts('Adjustment on no parcel', lambda key: adjustments[key].label(), a.stranded, b.stranded)

    stored_unallocated: dict[Any, Decimal] = {sale.id: sale.quantity for sale in facts.sales}
    for row in stored_holding.sales:
        stored_unallocated[row.sale_id] = stored_unallocated.get(row.sale_id, Decimal('0')) - row.quantity
    for sale in facts.sales:
        s = max(stored_unallocated.get(sale.id, Decimal('0')), Decimal('0'))
        r = replayed.unallocated.get(sale.id, Decimal('0'))
        if s != r:
            differ('Units allocated to no parcel', sale.label(), s, r)

    report.shape_differences = sum(((a.shape - b.shape) + (b.shape - a.shape)).values())
    report.respread = [
        f'{adjustment.label()} gave {pinned:f} to {buy.label()}; spread again it would give {fresh:f}'
        for adjustment, buy, pinned, fresh in replayed.respread]
    return report


def _show(value: Any) -> str:
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f')
    if isinstance(value, list):
        return ', '.join(_show(item) for item in value) or 'none'
    return str(value)


def check(account: 'Account', instrument: 'Instrument | None' = None) -> Report:
    """Replay `account`'s holding, or one instrument's, and set it against what is stored.

    Writes nothing.
    """
    facts = facts_module.load(account, instrument)
    return compare(facts, stored(account, instrument), replay(facts))


def verify(instrument: 'Instrument') -> bool:
    """Check one instrument and record on it whether any figure differs. Returns that."""
    from share_dinkum_app.models import Instrument

    differ = check(instrument.account, instrument).count > 0
    Instrument.objects.filter(pk=instrument.pk).update(holdings_differ=differ)
    instrument.holdings_differ = differ
    return differ
