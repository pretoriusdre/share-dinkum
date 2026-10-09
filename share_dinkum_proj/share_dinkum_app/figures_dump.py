"""Every figure the app works out for a portfolio, as sorted CSV tables.

A refactor that must not change any figure is checked by dumping before and after it and
diffing the two folders. So the dump leaves out whatever would differ for the same data:

* No ids. A row is named by a natural key (instrument, dates, quantities, legacy id), and an id
  found inside text is replaced by the key of the record it names.
* No timestamps, and rows are sorted.
* Decimals are written exactly as computed, without an exponent.

Reading figures writes: a fiscal year is created when first asked for, and a missing exchange
rate leaves a placeholder. So `dump` works inside a transaction it always rolls back, with the
market data fetches stubbed out, as the tests do. `date.today()` is not pinned, so take both
dumps on the same day.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import ExitStack, contextmanager
import csv
from dataclasses import fields, is_dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pandas as pd
from django.db import models, transaction
from djmoney.models.fields import CurrencyField
from djmoney.money import Money

from share_dinkum_app import cgt, income, yfinanceinterface

if TYPE_CHECKING:
    from share_dinkum_app.models import Account

#: A table: its header, then its rows.
Table = list[list[str]]

UUID_PATTERN = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')

#: Stand-in for an id the dump has no key for.
UNKNOWN_ID = '<id>'


# --- offline -------------------------------------------------------------------------------

#: What each market data fetch returns while the dump runs: nothing, as if Yahoo were down.
OFFLINE_FETCHES: dict[str, Callable[..., Any]] = {
    'get_exchange_rate': lambda *args, **kwargs: None,
    'get_current_price': lambda *args, **kwargs: None,
    'get_exchange_rate_history': lambda *args, **kwargs: pd.DataFrame(),
    'get_instrument_price_history': lambda *args, **kwargs: pd.DataFrame(),
}


@contextmanager
def offline() -> Iterator[None]:
    """Stub out every market data fetch, so a dump reads no network and is repeatable."""
    from unittest.mock import patch

    with ExitStack() as stack:
        for name, stub in OFFLINE_FETCHES.items():
            stack.enter_context(patch.object(yfinanceinterface, name, stub))
        yield


# --- natural keys --------------------------------------------------------------------------

def _text(value: Any) -> str:
    """One cell: exact, and the same for the same data."""
    if value is None:
        return ''
    if isinstance(value, Money):
        return f'{_text(value.amount)} {value.currency}'
    if isinstance(value, Decimal):
        return format(value, 'f')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, models.Model):
        name = getattr(value, 'name', None)
        return str(name) if name is not None else UNKNOWN_ID
    return str(value)


class Keys:
    """Natural keys for the records of one account, and their ids to translate from."""

    def __init__(self, account: 'Account') -> None:
        self.account = account
        self.by_id: dict[str, str] = {}

    def key(self, obj: models.Model) -> str:
        """The record's natural key, remembered so its id can be translated."""
        pk = str(obj.pk)
        if pk not in self.by_id:
            self.by_id[pk] = self._make(obj)
        return self.by_id[pk]

    def _make(self, obj: Any) -> str:
        from share_dinkum_app import models as m

        parts: list[Any]
        if isinstance(obj, m.Account):
            parts = ['account']
        elif isinstance(obj, m.Instrument):
            parts = [obj.name]
        elif isinstance(obj, m.Trade):
            parts = [type(obj).__name__, obj.instrument.name, obj.date, obj.quantity, obj.legacy_id]
        elif isinstance(obj, m.Parcel):
            parts = [self.key(obj.buy), obj.activation_date, obj.deactivation_date, obj.parcel_quantity,
                     obj.sale_date]
        elif isinstance(obj, m.SellAllocation):
            parts = [self.key(obj.sell), self.key(obj.parcel), obj.quantity]
        elif isinstance(obj, m.ShareSplit):
            parts = ['ShareSplit', obj.instrument.name, obj.date]
        elif isinstance(obj, m.CostBaseAdjustment):
            parts = ['CostBaseAdjustment', obj.instrument.name, obj.financial_year_end_date, obj.legacy_id]
        elif isinstance(obj, m.CostBaseAdjustmentAllocation):
            parts = [self.key(obj.cost_base_adjustment), self.key(obj.parcel), obj.activation_date,
                     obj.deactivation_date]
        elif isinstance(obj, (m.Dividend, m.Distribution)):
            parts = [type(obj).__name__, obj.instrument.name, obj.date, obj.legacy_id]
        elif isinstance(obj, m.AttributionStatement):
            parts = ['AttributionStatement', obj.instrument.name, obj.financial_year_end_date]
        else:
            raise TypeError(f'figures_dump has no natural key for {type(obj).__name__}.')
        return ' | '.join(_text(part) for part in parts)

    def translate(self, value: Any) -> str:
        """`value` as a cell, with any id in it replaced by the key of the record it names."""
        if isinstance(value, UUID):
            return self.by_id.get(str(value), UNKNOWN_ID)
        text = _text(value)
        return UUID_PATTERN.sub(lambda match: self.by_id.get(match.group(0), UNKNOWN_ID), text)


#: The records an id in the figures can name, keyed before anything is translated.
KEYED_MODELS = (
    'Instrument', 'Buy', 'Sell', 'Parcel', 'SellAllocation', 'ShareSplit', 'CostBaseAdjustment',
    'CostBaseAdjustmentAllocation', 'Dividend', 'Distribution', 'AttributionStatement',
)


def _rows_of(account: 'Account', model_name: str) -> 'models.QuerySet[Any]':
    from django.apps import apps

    model = apps.get_model('share_dinkum_app', model_name)
    return model.objects.filter(account=account)


def calculated_fields(model: type[models.Model]) -> list['models.Field[Any, Any]']:
    """The model's stored `calculated_*` fields, without django-money's currency columns."""
    return [field for field in model._meta.fields
            if field.name.startswith('calculated_') and not isinstance(field, CurrencyField)]


def models_with_calculated_fields() -> list[type[models.Model]]:
    from django.apps import apps

    return [model for model in apps.get_app_config('share_dinkum_app').get_models()
            if calculated_fields(model)]


# --- the tables ----------------------------------------------------------------------------

def _table(header: list[str], rows: Iterable[list[str]]) -> Table:
    return [header, *sorted(rows)]


def _dataclass_table(items: Iterable[Any], keys: Keys, skip: tuple[str, ...] = ()) -> Table:
    items = list(items)
    if not items:
        return [[]]
    assert is_dataclass(items[0])
    names = [f.name for f in fields(items[0]) if f.name not in skip]
    return _table(names, ([keys.translate(getattr(item, name)) for name in names] for item in items))


def _parcels(account: 'Account', keys: Keys) -> Table:
    from share_dinkum_app.models import Parcel

    parcels = Parcel.objects.filter(account=account).select_related('buy__instrument', 'parent_parcel__buy')
    return _table(
        ['parcel', 'parent', 'cumulative_split_multiplier', 'is_active'],
        ([keys.key(p), keys.key(p.parent_parcel) if p.parent_parcel else '',
          _text(p.cumulative_split_multiplier), _text(p.is_active)] for p in parcels))


def _sell_allocations(account: 'Account', keys: Keys) -> Table:
    from share_dinkum_app.models import SellAllocation

    allocations = SellAllocation.objects.filter(account=account).select_related(
        'sell__instrument', 'parcel__buy__instrument')
    return _table(['allocation', 'is_active'], ([keys.key(a), _text(a.is_active)] for a in allocations))


def _adjustment_allocations(account: 'Account', keys: Keys) -> Table:
    from share_dinkum_app.models import CostBaseAdjustmentAllocation

    allocations = CostBaseAdjustmentAllocation.objects.filter(account=account).select_related(
        'cost_base_adjustment__instrument', 'parcel__buy__instrument')
    return _table(['allocation', 'cost_base_increase', 'is_active'],
                  ([keys.key(a), _text(a.cost_base_increase), _text(a.is_active)] for a in allocations))


def _calculated(account: 'Account', keys: Keys) -> Table:
    from share_dinkum_app.models import Account

    rows: list[list[str]] = []
    for model in models_with_calculated_fields():
        records: Iterable[Any] = [account] if model is Account else model._default_manager.filter(account=account)
        for record in records:
            for field in calculated_fields(model):
                rows.append([model.__name__, keys.key(record), field.name,
                             keys.translate(getattr(record, field.name))])
    return _table(['model', 'record', 'field', 'value'], rows)


def _schedules(account: 'Account', events: list[cgt.CGTEvent]) -> Table:
    from share_dinkum_app.models import CapitalLossCarryForward

    years = {event.fiscal_year for event in events if event.fiscal_year}
    years |= {str(loss.fiscal_year) for loss in CapitalLossCarryForward.objects.filter(account=account)}

    rows: list[list[str]] = []
    for year in [*sorted(years), None]:
        schedule = cgt.build_schedule(account, year, every_year=events)
        label = year or 'ALL'
        for f in fields(schedule):
            if f.name == 'lines':
                continue
            values = getattr(schedule, f.name)
            for value in (values if isinstance(values, list) else [values]):
                rows.append([label, '', f.name, _text(value)])
        for line in schedule.lines:
            for f in fields(line):
                if f.name != 'category':
                    rows.append([label, line.category, f.name, _text(getattr(line, f.name))])
    return _table(['year', 'category', 'field', 'value'], rows)


def _income_figures(summary: income.IncomeSummary) -> Table:
    rows: list[list[str]] = []
    for year in summary.years():
        rows += [[year, key, _text(value)] for key, value in summary.figures(year).items()]
    for year, warnings in summary.warnings.items():
        rows += [[year, 'warning', text] for text in warnings]
    return _table(['year', 'figure', 'value'], rows)


def collect(account: 'Account') -> dict[str, Table]:
    """Every figure for `account`, by table name. Reads with the network stubbed out.

    Writes what reading writes (fiscal years, placeholder rates), so call it inside a
    transaction that is rolled back, as `dump` does.
    """
    keys = Keys(account)
    for model_name in KEYED_MODELS:
        for record in _rows_of(account, model_name):
            keys.key(record)

    events = cgt.all_events(account)
    summary = income.build(account)
    return {
        'parcels': _parcels(account, keys),
        'sell_allocations': _sell_allocations(account, keys),
        'adjustment_allocations': _adjustment_allocations(account, keys),
        'calculated': _calculated(account, keys),
        'cgt_events': _dataclass_table(events, keys),
        'cgt_schedule': _schedules(account, events),
        'income_payments': _dataclass_table(summary.payments, keys),
        'income_trust_lines': _dataclass_table(summary.trust_lines, keys),
        'income_figures': _income_figures(summary),
    }


def dump(account: 'Account') -> dict[str, Table]:
    """`collect`, offline, inside a transaction that is always rolled back."""
    with transaction.atomic():
        try:
            with offline():
                return collect(account)
        finally:
            transaction.set_rollback(True)


def write(tables: dict[str, Table], folder: Path) -> list[Path]:
    """Write each table to `<folder>/<name>.csv`. Returns the paths written."""
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, table in tables.items():
        path = folder / f'{name}.csv'
        with open(path, 'w', newline='', encoding='utf-8') as f:
            csv.writer(f, lineterminator='\n').writerows(table)
        written.append(path)
    return written


def read(folder: Path) -> dict[str, Table]:
    """Tables written by `write`, by name."""
    tables: dict[str, Table] = {}
    for path in sorted(folder.glob('*.csv')):
        with open(path, newline='', encoding='utf-8') as f:
            tables[path.stem] = [row for row in csv.reader(f)] or [[]]
    return tables
