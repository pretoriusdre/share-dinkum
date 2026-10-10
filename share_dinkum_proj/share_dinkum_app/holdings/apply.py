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
from typing import Any

from share_dinkum_app.holdings.state import AdjustmentRow, Holding, Lineage, SaleRow

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
    #: lineage -> the replayed fields, for a parcel the stored holding lacks. Parents first.
    create_parcels: dict[Lineage, dict[str, Any]] = field(default_factory=dict)
    #: stored ref -> the fields that change.
    update_parcels: dict[Any, dict[str, Any]] = field(default_factory=dict)
    #: stored refs the replay does not make.
    delete_parcels: list[Any] = field(default_factory=list)
    #: (sale id, parcel lineage, quantity) to add.
    create_sales: list[tuple[Any, Lineage, Decimal]] = field(default_factory=list)
    #: stored ref -> new quantity.
    update_sales: dict[Any, Decimal] = field(default_factory=dict)
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


def _plan_parcels(result: Plan, stored: Holding, replayed: Holding,
                  stored_lineages: dict[Any, Lineage], replayed_lineages: dict[Any, Lineage]) -> None:
    stored_by_lineage = {lineage: ref for ref, lineage in stored_lineages.items()}
    replayed_by_lineage = {lineage: ref for ref, lineage in replayed_lineages.items()}
    for lineage, ref in sorted(replayed_by_lineage.items(), key=lambda item: len(item[0])):
        wanted = _parcel_fields(replayed, replayed_lineages, ref)
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
    have: dict[tuple[Any, Lineage], list[SaleRow]] = defaultdict(list)
    for row in stored.sales:
        have[(row.sale_id, stored_lineages[row.parcel_ref])].append(row)
    want: dict[tuple[Any, Lineage], Decimal] = defaultdict(Decimal)
    for row in replayed.sales:
        want[(row.sale_id, replayed_lineages[row.parcel_ref])] += row.quantity

    for key, quantity in want.items():
        rows = have.pop(key, [])
        if not rows:
            result.create_sales.append((key[0], key[1], quantity))
            continue
        others = sum((row.quantity for row in rows[1:]), Decimal('0'))
        if rows[0].quantity + others != quantity:
            result.update_sales[rows[0].ref] = quantity - others
    result.delete_sales = [row.ref for rows in have.values() for row in rows]


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
