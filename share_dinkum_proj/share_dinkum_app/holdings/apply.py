"""What writing a replayed holding over the stored one would change.

Rows are matched by lineage (see `state`), so a parcel the replay also makes is updated in
place and keeps its id; snapshots, log entries and exports refer to those ids. A holding that
already agrees with its replay plans no change at all.

Adjustment parts are matched on (adjustment, parcel lineage), and only the active ones decide
what changes. Deactivated parts are history: one entered after a sale was spread straight onto
the parcels the sale left, while the replay spreads it before the sale and divides it at the
sale. The amounts in force are the same, so the stored history is kept as it is.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from share_dinkum_app.holdings.state import AdjustmentRow, Holding, Lineage, SaleRow

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, Instrument

#: The parcel fields a rebuild sets.
PARCEL_FIELDS = ('parent', 'activation', 'deactivation', 'quantity', 'multiplier', 'sale_date')


@dataclass(frozen=True)
class NewPart:
    adjustment_id: Any
    lineage: Lineage
    amount: Decimal
    activation: date | None


@dataclass
class Plan:
    #: Every stored parcel's id, by lineage, so the writer can find what it updates.
    parcel_refs: dict[Lineage, Any] = field(default_factory=dict)
    #: Every replayed parcel's fields, by lineage.
    wanted_parcels: dict[Lineage, dict[str, Any]] = field(default_factory=dict)
    #: lineage -> the replayed fields, for a parcel the stored holding lacks. Parents first.
    create_parcels: dict[Lineage, dict[str, Any]] = field(default_factory=dict)
    #: stored ref -> the fields that change.
    update_parcels: dict[Any, dict[str, Any]] = field(default_factory=dict)
    #: stored refs the replay does not make.
    delete_parcels: list[Any] = field(default_factory=list)
    #: (sale id, parcel lineage, quantity) to add.
    create_sales: list[tuple[Any, Lineage, Decimal]] = field(default_factory=list)
    #: stored ref -> the fields that change: 'parcel' (a lineage) and 'quantity'.
    update_sales: dict[Any, dict[str, Any]] = field(default_factory=dict)
    delete_sales: list[Any] = field(default_factory=list)
    create_parts: list[NewPart] = field(default_factory=list)
    #: stored ref -> the fields that change ('amount', 'deactivation').
    update_parts: dict[Any, dict[str, Any]] = field(default_factory=dict)
    delete_parts: list[Any] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not any((self.create_parcels, self.update_parcels, self.delete_parcels, self.create_sales,
                        self.update_sales, self.delete_sales, self.create_parts, self.update_parts,
                        self.delete_parts))

    def summary(self) -> str:
        return (f'parcels +{len(self.create_parcels)} ~{len(self.update_parcels)} -{len(self.delete_parcels)}, '
                f'sale allocations +{len(self.create_sales)} ~{len(self.update_sales)} -{len(self.delete_sales)}, '
                f'adjustment parts +{len(self.create_parts)} ~{len(self.update_parts)} -{len(self.delete_parts)}')


def _parcel_fields(holding: Holding, lineages: dict[Any, Lineage], ref: Any) -> dict[str, Any]:
    parcel = holding.parcels[ref]
    return {
        'parent': lineages.get(parcel.parent_ref),
        'activation': parcel.activation,
        'deactivation': parcel.deactivation,
        'quantity': parcel.quantity,
        'multiplier': parcel.multiplier,
        'sale_date': parcel.sale_date,
    }


def _creation_order(lineage: Lineage) -> tuple[Any, ...]:
    """Sorts parents before children, and siblings by date then place."""
    return (len(lineage), tuple((str(made), place) for made, place in lineage[1:]))


def _plan_parcels(result: Plan, stored: Holding, replayed: Holding,
                  stored_lineages: dict[Any, Lineage], replayed_lineages: dict[Any, Lineage]) -> None:
    stored_by_lineage = {lineage: ref for ref, lineage in stored_lineages.items()}
    replayed_by_lineage = {lineage: ref for ref, lineage in replayed_lineages.items()}
    result.parcel_refs = dict(stored_by_lineage)
    # Parents before children, and siblings in their place: a stored parcel's place among its
    # siblings is read from the order of their ids, so they must be made in that order.
    for lineage, ref in sorted(replayed_by_lineage.items(), key=lambda item: _creation_order(item[0])):
        wanted = _parcel_fields(replayed, replayed_lineages, ref)
        result.wanted_parcels[lineage] = wanted
        if lineage not in stored_by_lineage:
            result.create_parcels[lineage] = wanted
            continue
        have = _parcel_fields(stored, stored_lineages, stored_by_lineage[lineage])
        changed = {name: wanted[name] for name in PARCEL_FIELDS if have[name] != wanted[name]}
        if changed:
            result.update_parcels[stored_by_lineage[lineage]] = changed
    result.delete_parcels = [ref for lineage, ref in stored_by_lineage.items() if lineage not in replayed_by_lineage]


def _plan_sales(result: Plan, stored: Holding, replayed: Holding,
                stored_lineages: dict[Any, Lineage], replayed_lineages: dict[Any, Lineage]) -> None:
    """Allocations are matched on (sale, buy), not on the parcel: that is the decision, and a
    rebuild that reshapes the parcel tree re-points an allocation rather than replacing it, so
    its id survives. Lodged snapshots match their rows on that id."""
    have: dict[tuple[Any, Any], list[SaleRow]] = defaultdict(list)
    for row in sorted(stored.sales, key=lambda row: row.ref):
        have[(row.sale_id, stored.parcels[row.parcel_ref].buy_id)].append(row)
    want: dict[tuple[Any, Any], list[tuple[Lineage, Decimal]]] = defaultdict(list)
    for row in replayed.sales:
        want[(row.sale_id, replayed.parcels[row.parcel_ref].buy_id)].append(
            (replayed_lineages[row.parcel_ref], row.quantity))

    for key, wanted in want.items():
        rows = have.pop(key, [])
        for index, (lineage, quantity) in enumerate(wanted):
            if index >= len(rows):
                result.create_sales.append((key[0], lineage, quantity))
                continue
            row = rows[index]
            changes: dict[str, Any] = {}
            if stored_lineages[row.parcel_ref] != lineage:
                changes['parcel'] = lineage
            if row.quantity != quantity:
                changes['quantity'] = quantity
            if changes:
                result.update_sales[row.ref] = changes
        result.delete_sales += [row.ref for row in rows[len(wanted):]]
    result.delete_sales += [row.ref for rows in have.values() for row in rows]


def _plan_parts(result: Plan, stored: Holding, replayed: Holding,
                stored_lineages: dict[Any, Lineage], replayed_lineages: dict[Any, Lineage]) -> None:
    active: dict[tuple[Any, Lineage], list[AdjustmentRow]] = defaultdict(list)
    inactive: dict[tuple[Any, Lineage], list[AdjustmentRow]] = defaultdict(list)
    for row in stored.adjustments:
        key = (row.adjustment_id, stored_lineages[row.parcel_ref])
        (active if row.active else inactive)[key].append(row)

    want: dict[tuple[Any, Lineage], Decimal] = defaultdict(Decimal)
    first_active: dict[tuple[Any, Lineage], AdjustmentRow] = {}
    ended: dict[tuple[Any, Lineage], date | None] = {}
    for row in replayed.adjustments:
        key = (row.adjustment_id, replayed_lineages[row.parcel_ref])
        if row.active:
            want[key] += row.amount
            first_active.setdefault(key, row)
        else:
            ended[key] = row.deactivation

    for key, amount in want.items():
        rows = active.pop(key, [])
        if not rows:
            revived = inactive.get(key)
            if revived:
                result.update_parts[revived[0].ref] = {'amount': amount, 'deactivation': None}
            else:
                result.create_parts.append(NewPart(key[0], key[1], amount, first_active[key].activation))
            continue
        others = sum((row.amount for row in rows[1:]), Decimal('0'))
        if rows[0].amount + others != amount:
            result.update_parts[rows[0].ref] = {'amount': amount - others}

    # Stored as in force, but not in the replay: ended by a later event, or not made at all.
    for key, rows in active.items():
        for row in rows:
            if key in ended:
                result.update_parts[row.ref] = {'deactivation': ended[key]}
            else:
                result.delete_parts.append(row.ref)


def plan(stored: Holding, replayed: Holding) -> Plan:
    """What it takes to turn `stored` into `replayed`, keeping every stored row that matches."""
    result = Plan()
    stored_lineages, replayed_lineages = stored.lineages(), replayed.lineages()
    _plan_parcels(result, stored, replayed, stored_lineages, replayed_lineages)
    _plan_sales(result, stored, replayed, stored_lineages, replayed_lineages)
    _plan_parts(result, stored, replayed, stored_lineages, replayed_lineages)
    return result


# --- writing -------------------------------------------------------------------------------

def write(account: 'Account', plan: Plan) -> None:
    """Make the stored holding what `plan` says, in the caller's transaction.

    In this order, so nothing is pointed at a row that does not exist yet or is about to go:
    allocations and parts the replay does not make are removed; parcels are created (parents
    first) and updated; allocations and parts are created and updated; then parcels the replay
    does not make are removed. Last, each split is linked to the parcels it made, and the stored
    figures of the instruments touched are worked out again.
    """
    from django.db import transaction
    from djmoney.money import Money

    from share_dinkum_app import recalculate
    from share_dinkum_app.models import (
        Buy, CostBaseAdjustmentAllocation, Instrument, Parcel, Sell, SellAllocation, ShareSplit,
    )

    from share_dinkum_app.holdings import live

    if plan.empty:
        return
    currency = str(account.currency)
    refs = dict(plan.parcel_refs)
    # What the writes touch, so only their stored figures are worked out again: a rebuild runs
    # on every change, and refreshing a whole instrument each time made an import quadratic.
    parcels: set[Any] = set()
    sells: set[Any] = set()
    buys: set[Any] = set()

    def allocation_rows(ids: list[Any]) -> list[tuple[Any, Any, Any]]:
        return list(SellAllocation.objects.filter(pk__in=ids).values_list('parcel_id', 'sell_id', 'buy_id'))

    # The writer's own saves must not start another rebuild of a half-written holding.
    with live.writing(), transaction.atomic():
        # Deleting an allocation clears its parcel's sale date if nothing else sells it. Where
        # the replay keeps that parcel sold, the date is set again below.
        removed = allocation_rows(plan.delete_sales)
        SellAllocation.objects.filter(pk__in=plan.delete_sales).delete()
        removed_parts = CostBaseAdjustmentAllocation.objects.filter(pk__in=plan.delete_parts)
        parcels.update(removed_parts.values_list('parcel_id', flat=True))
        removed_parts.delete()
        lineage_of = {ref: lineage for lineage, ref in plan.parcel_refs.items()}
        for parcel_id, sell_id, _ in removed:
            parcels.add(parcel_id)
            sells.add(sell_id)
            wanted = plan.wanted_parcels.get(lineage_of.get(parcel_id, ()))
            if wanted is not None and parcel_id not in plan.update_parcels:
                Parcel.objects.filter(pk=parcel_id).update(sale_date=wanted['sale_date'])

        for lineage, fields in plan.create_parcels.items():
            parcel = Parcel(
                account=account, buy_id=lineage[0], parent_parcel_id=refs.get(fields['parent']),
                parcel_quantity=fields['quantity'], cumulative_split_multiplier=fields['multiplier'],
                activation_date=fields['activation'], deactivation_date=fields['deactivation'],
                sale_date=fields['sale_date'])
            parcel.save()
            refs[lineage] = parcel.pk
            parcels.add(parcel.pk)

        attributes = {'activation': 'activation_date', 'deactivation': 'deactivation_date',
                      'quantity': 'parcel_quantity', 'multiplier': 'cumulative_split_multiplier',
                      'sale_date': 'sale_date'}
        for ref, changes in plan.update_parcels.items():
            parcel = Parcel.objects.get(pk=ref)
            for name, value in changes.items():
                if name == 'parent':
                    parcel.parent_parcel_id = refs.get(value)
                else:
                    setattr(parcel, attributes[name], value)
            parcel.save()
            parcels.add(ref)

        for sale_id, lineage, quantity in plan.create_sales:
            SellAllocation(account=account, sell_id=sale_id, parcel_id=refs[lineage], quantity=quantity,
                           _creation_handled=True).save()
            parcels.add(refs[lineage])
            sells.add(sale_id)
        before = {row[0]: row[1:] for row in SellAllocation.objects.filter(
            pk__in=list(plan.update_sales)).values_list('id', 'parcel_id', 'sell_id')}
        for ref, changes in plan.update_sales.items():
            old_parcel, sell_id = before[ref]
            # Parcel and quantity are structural, so a save refuses them; the rebuild is what
            # works them out now.
            columns: dict[str, Any] = {}
            if 'parcel' in changes:
                columns['parcel_id'] = refs[changes['parcel']]
            if 'quantity' in changes:
                columns['quantity'] = changes['quantity']
            SellAllocation.objects.filter(pk=ref).update(**columns)
            parcels.update({old_parcel, columns.get('parcel_id', old_parcel)})
            sells.add(sell_id)

        for part in plan.create_parts:
            CostBaseAdjustmentAllocation(
                account=account, cost_base_adjustment_id=part.adjustment_id, parcel_id=refs[part.lineage],
                cost_base_increase=Money(part.amount, currency), activation_date=part.activation).save()
            parcels.add(refs[part.lineage])
        for ref, changes in plan.update_parts.items():
            allocation = CostBaseAdjustmentAllocation.objects.get(pk=ref)
            if 'amount' in changes:
                allocation.cost_base_increase = Money(changes['amount'], currency)
            if 'deactivation' in changes:
                allocation.deactivation_date = changes['deactivation']
            allocation.save()
            parcels.add(allocation.parcel_id)

        if plan.delete_parcels:
            gone = Parcel.objects.filter(pk__in=plan.delete_parcels)
            buys.update(gone.values_list('buy_id', flat=True))
            # Their own history goes with them: parts long since moved on, which block the delete.
            CostBaseAdjustmentAllocation.objects.filter(parcel__in=gone).delete()
            gone.delete()
            parcels.difference_update(plan.delete_parcels)

        touched = Parcel.objects.filter(pk__in=parcels)
        buys.update(touched.values_list('buy_id', flat=True))
        sells.update(SellAllocation.objects.filter(parcel__in=touched, is_active=True).values_list('sell_id', flat=True))
        instruments = list(Instrument.objects.filter(buy__in=buys).distinct())
        for split in ShareSplit.objects.filter(account=account, instrument__in=instruments):
            made = split.parcels_created()
            if set(split.affected_parcels.all()) != set(made):
                split.affected_parcels.set(made)
                split.save()
        recalculate.parcels(touched)
        for sell in Sell.objects.filter(pk__in=sells):
            sell.save()
        for buy in Buy.objects.filter(pk__in=buys):
            buy.save()
        for instrument in instruments:
            Instrument.objects.get(pk=instrument.pk).save()


def rebuild(account: 'Account', instrument: 'Instrument | None' = None) -> Plan:
    """Work `account`'s holding, or one instrument's, out again and write it. Returns what was
    changed."""
    from share_dinkum_app.holdings import facts, replay, state

    result = plan(state.stored(account, instrument), replay.replay(facts.load(account, instrument)).holding)
    write(account, result)
    return result
