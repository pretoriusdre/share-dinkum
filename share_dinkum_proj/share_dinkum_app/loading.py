import pandas as pd

from datetime import date, datetime
from decimal import Decimal
import shutil
import sqlite3
from tqdm import tqdm
from pathlib import Path

from django.apps import apps
from django.db.models import DecimalField, FileField
from django.db import connections, transaction
from django.core.exceptions import FieldDoesNotExist, ObjectDoesNotExist, ValidationError
from django.conf import settings
from django.core.management import call_command


from djmoney.models.fields import MoneyField
from djmoney.money import Money

import share_dinkum_app
from share_dinkum_app import backup as backup_module, excelinterface, recalculate
from share_dinkum_app.choices import SellStrategy
from share_dinkum_app import yfinanceinterface
from django.db import models

import share_dinkum_app.models as app_models
from share_dinkum_app.utils import convert_to_decimal_field, save_with_logging, process_filefield
from share_dinkum_app.utils.signal_helpers import disconnect_app_signals, reconnect_app_signals


import logging
logger = logging.getLogger(__name__)


def make_tz_naive(df):
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].dt.tz_localize(None)
    return df


def restore_blank_text_defaults(df, model):
    """Replace None with the default (or '') in non-null CharField and TextField columns.

    Excel cannot distinguish an empty string from an empty cell, so a blank optional text
    field such as `AppUser.email` would otherwise fail its NOT NULL constraint. Other
    columns are left as None, so a missing date or quantity still fails.
    """
    for col in df.columns:
        if col.startswith('lookup_') or '__' in col:
            continue
        try:
            field = model._meta.get_field(col)
        except Exception:
            continue
        if field.is_relation or field.null:
            continue
        if not isinstance(field, (models.CharField, models.TextField)):
            continue

        blank_value = field.get_default() if field.has_default() else ''
        if blank_value is None:
            blank_value = ''
        df[col] = df[col].apply(lambda v: blank_value if v is None else v)

    return df


def normalise_legacy_id(value):
    """A legacy id as text. Excel gives a number, and a float if its column has a blank."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value)


def normalise_choice(model, field, value):
    """The stored key for a choice given as its key, its label, or either in another case."""
    if not isinstance(value, str):
        return value
    choices = list(field.flatchoices)
    if any(value == key for key, _ in choices):
        return value
    folded = value.strip().casefold()
    for key, label in choices:
        if folded in (str(key).casefold(), str(label).casefold()):
            return key
    listed = ', '.join(str(key) for key, _ in choices[:12]) + (', ...' if len(choices) > 12 else '')
    raise ValueError(
        f'{model.__name__} {field.name} cannot be "{value}". It must be one of: {listed}.')


def normalise_date(model, field, value):
    """A date from a date, a datetime, or text such as 2024-07-01."""
    if value is None:
        return None
    try:
        return field.to_python(value)
    except ValidationError as exc:
        raise ValueError(
            f'{model.__name__} {field.name} cannot read "{value}" as a date.') from exc


def normalise_cells(df, model):
    """Legacy ids as text, choices as their keys and dates as dates. Repeated ids refused.

    An unrecognised choice used to be stored as typed: a strategy of "fifo" matched no
    branch and took parcels in no particular order.
    """
    for column in df.columns:
        if column == 'legacy_id' or column.startswith('lookup_legacy'):
            df[column] = df[column].apply(normalise_legacy_id)
            continue
        try:
            field = model._meta.get_field(column)
        except FieldDoesNotExist:
            continue
        if field.is_relation:
            continue
        if field.choices:
            df[column] = df[column].apply(lambda v, f=field: normalise_choice(model, f, v))
        elif isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField):
            df[column] = df[column].apply(lambda v, f=field: normalise_date(model, f, v))

    if 'legacy_id' in df.columns:
        ids = df['legacy_id'].dropna()
        repeated = sorted(set(ids[ids.duplicated()]))
        if repeated:
            raise ValueError(
                f'{model.__name__} has more than one row with legacy_id '
                f'{", ".join(repeated)}. Rows are matched to records by it, so each needs its '
                f'own; otherwise the later row overwrites the earlier one.')
    return df


def fill_blank_currencies(model, record, blank_columns):
    """Give a new record's money the instrument's currency where the file leaves it blank.

    The column default is AUD, so a US dollar price with no currency was taken as dollars
    Australian.
    """
    instrument = record.get('instrument')
    if instrument is None and record.get('instrument_id'):
        instrument = app_models.Instrument.objects.filter(pk=record['instrument_id']).first()
    if instrument is None:
        return
    for field in model._meta.fields:
        if not isinstance(field, MoneyField):
            continue
        currency_column = f'{field.name}_currency'
        if field.name in record and (
                currency_column in blank_columns or currency_column not in record):
            record[currency_column] = str(instrument.currency)


def model_to_queryset(model, account=None):
    fields = [f.name for f in model._meta.fields]
    related_fields = [f.name for f in model._meta.fields if f.is_relation]
    queryset = model.objects.select_related(*related_fields).all()
    if account:
        queryset = queryset.filter(account=account)
    return queryset


def queryset_to_df(queryset):

    model = queryset.model

    fields = [f.name for f in model._meta.fields]
    related_fields = [f.name for f in model._meta.fields if f.is_relation]

    # Include model properties (calculated fields)
    properties = [attr for attr in dir(model) if isinstance(getattr(model, attr), property)]

    data = []
    for obj in queryset:
        record = {}
        for field_name in fields:
            field_value = getattr(obj, field_name)
            if field_name in related_fields:
                # Get the related object's 'name' attribute if the field is a related field
                if field_value is not None:
                    if hasattr(field_value, 'name'):
                        record[field_name + '__name'] = getattr(field_value, 'name')
                    else:
                        record[field_name + '_id'] = field_value.id
            else:
                record[field_name] = field_value

        data.append(record)

    df = pd.DataFrame(data)
    df = make_tz_naive(df)

    hidden_columns = ['password']
    for column in hidden_columns:
        if column in df.columns:
            df = df.drop(columns=column)

    return df


class DataLoader():

    def __init__(self, account=None, input_file=None):
        """Load `input_file` into `account`, or, if None, into the portfolio the file names.

        `account=None` is a restore: the file's own Account row is loaded and used, keeping
        its original id.
        """
        self.input_file = input_file
        self.account = account

        if self.input_file:
            self.mapping = excelinterface.get_all_tables_in_excel(self.input_file)
            if self.account is None:
                self.account = self.account_named_by_file()
            self.load_all_tables()

    def account_named_by_file(self):
        """The existing Account matching the file's single Account row, or None.

        Raises ValueError if the file has no Account row or more than one.
        """
        df = self.mapping.get('Account')
        if df is None or df.empty or 'id' not in df.columns:
            raise ValueError(
                'This file does not name a portfolio, so there is nothing to load it into. '
                'Pass account= to say which portfolio it belongs to.')
        if len(df) > 1:
            raise ValueError(
                'This file names more than one portfolio, so it cannot be restored on its '
                'own. Pass account= to say which one is meant.')
        return app_models.Account.objects.filter(id=df.iloc[0]['id']).first()

    @classmethod
    def get_model_load_order(cls):

        # TODO work out the ordering based on the model dependencies
        model_load_order = {
            
            # Published national data, belonging to no portfolio, and depended on by
            # nothing else, so it loads first and stands alone.
            'CPIIndex': share_dinkum_app.models.CPIIndex,

            'AppUser': share_dinkum_app.models.AppUser,
            'FiscalYearType': share_dinkum_app.models.FiscalYearType,
            'FiscalYear': share_dinkum_app.models.FiscalYear,
            'Account': share_dinkum_app.models.Account,
            'LogEntry': share_dinkum_app.models.LogEntry,
            'CurrentExchangeRate': share_dinkum_app.models.CurrentExchangeRate,
            'ExchangeRate': share_dinkum_app.models.ExchangeRate,
            'ResidencyPeriod': share_dinkum_app.models.ResidencyPeriod,
            'Market': share_dinkum_app.models.Market,
            'Instrument': share_dinkum_app.models.Instrument,
            'InstrumentValuation': share_dinkum_app.models.InstrumentValuation,
            'InstrumentPriceHistory': share_dinkum_app.models.InstrumentPriceHistory,
            'Buy': share_dinkum_app.models.Buy,
            'Sell': share_dinkum_app.models.Sell,
            'Parcel': share_dinkum_app.models.Parcel,
            'SellAllocation': share_dinkum_app.models.SellAllocation,
            'ShareSplit': share_dinkum_app.models.ShareSplit,
            'CostBaseAdjustment': share_dinkum_app.models.CostBaseAdjustment,
            'CostBaseAdjustmentAllocation': share_dinkum_app.models.CostBaseAdjustmentAllocation,
            'AttributionStatement': share_dinkum_app.models.AttributionStatement,
            'AttributionComponent': share_dinkum_app.models.AttributionComponent,
            'Dividend': share_dinkum_app.models.Dividend,
            'Distribution': share_dinkum_app.models.Distribution,
            'DataExport': share_dinkum_app.models.DataExport,
            'CapitalLossCarryForward': share_dinkum_app.models.CapitalLossCarryForward,
            'CGTReturnSnapshot': share_dinkum_app.models.CGTReturnSnapshot,
            'CGTReturnSnapshotRow': share_dinkum_app.models.CGTReturnSnapshotRow,
        }

        return model_load_order.values()
    

    #: From an import template these are loaded as one timeline in date order, not table by
    #: table. See `load_timeline`.
    TIMELINE_TABLES = ('Sell', 'SellAllocation', 'ShareSplit', 'CostBaseAdjustment')

    def is_template(self):
        """Whether the app derives parcels and allocations from this file (an import template).

        An export carries them already, and says so with a `_creation_handled` column.
        """
        return not any(
            df is not None and '_creation_handled' in df.columns
            for df in self.mapping.values())

    def load_all_tables(self):
        """Load every table in the file in one transaction, so a failure loads nothing."""

        model_load_order = self.get_model_load_order()
        template = self.is_template()
        self._had_rows = self._models_with_rows()

        with transaction.atomic():
            for model in model_load_order:
                table_name = model.__name__

                if table_name in ['LogEntry']:
                    continue  # Skip loading LogEntry as ContentType as a name property, not field. Hard to loookup by name.

                if table_name == 'DataExport':
                    # An export lists its own DataExport row, without its file, and the files
                    # of earlier ones, which a load does not bring. Loaded, the first deleted
                    # the stored export and started a new one mid-load.
                    continue

                if template and table_name in self.TIMELINE_TABLES:
                    if table_name == 'Sell':
                        self.load_timeline()
                    continue

                df = self.mapping.get(table_name)
                if df is not None:
                    logger.info(f"Loading {table_name}")
                    self.load_table_to_model(model=model, df=df)

                # On a restore the portfolio does not exist until its own row is loaded.
                # Adopt it the moment it does, so every table after this one is checked
                # against the account the file actually refers to rather than a new one
                # created alongside it.
                if self.account is None and model is app_models.Account:
                    self.account = self.account_named_by_file()
                    if self.account is None:
                        raise ValueError(
                            'The Account row in this file did not load, so there is no '
                            'portfolio to attach the rest of it to.')
                    logger.info('Restoring into portfolio %s', self.account)
                    self._had_rows = self._models_with_rows()

            if not template and self.account is not None:
                # An export's derived rows arrive already handled, so the saves that would
                # have kept the stored figures current (an instrument's holding, a parcel's
                # sold flag) ran before the rows they count had loaded.
                recalculate.account(self.account)

    @staticmethod
    def _matched_only_by_legacy_id(model):
        """Whether a row of `model` without an id can be matched to a record only by legacy_id."""
        names = {field.name for field in model._meta.fields}
        return ('legacy_id' in names and 'account' in names
                and not any(getattr(c, 'fields', None) for c in model._meta.constraints))

    def _models_with_rows(self):
        """Models matched only by legacy_id that already had records in the account."""
        if self.account is None:
            return set()
        return {
            model for model in self.get_model_load_order()
            if self._matched_only_by_legacy_id(model)
            and model.objects.filter(account=self.account).exists()
        }

    def load_timeline(self):
        """Load sales (each with its pinned allocations), splits and adjustments in date order.

        Each is worked out from the holding as it stood on its date: a sale after a split has
        to find the split parcels, and a sale before it the parcels as they were. Loaded
        table by table, every sale was allocated before any split, in row order, and a
        "minimise capital gain" sale ranked parcels before their adjustments existed.
        """
        prepared = {}
        for name in self.TIMELINE_TABLES:
            df = self.mapping.get(name)
            if df is not None and not df.empty:
                model = apps.get_model('share_dinkum_app', name)
                prepared[name] = (model, *self.prepare_table(model, df))

        self.check_pinned_allocations(prepared)

        # On one date: a split first, since a sale on its ex-date is in post-split units, and
        # an adjustment last, since it is spread over the year's holdings.
        events = []
        for rank, name, date_column in ((0, 'ShareSplit', 'date'), (1, 'Sell', 'date'),
                                        (2, 'CostBaseAdjustment', 'financial_year_end_date')):
            if name not in prepared:
                continue
            _, df, _ = prepared[name]
            for position, (index, row) in enumerate(df.iterrows()):
                events.append((row.get(date_column) or date.min, rank, position, name, index))
        events.sort(key=lambda event: event[:3])

        pinned, loose = {}, []
        if 'SellAllocation' in prepared:
            _, allocations, _ = prepared['SellAllocation']
            sells_in_file = set()
            if 'Sell' in prepared and 'legacy_id' in prepared['Sell'][1].columns:
                sells_in_file = set(prepared['Sell'][1]['legacy_id'].dropna())
            for index, row in allocations.iterrows():
                sell = row.get('lookup_legacy_sell')
                if sell in sells_in_file:
                    pinned.setdefault(sell, []).append(index)
                else:
                    loose.append(index)

        def load(name, indexes):
            model, df, blank = prepared[name]
            logger.info('Loading %s %s row(s)', len(indexes), name)
            self.load_rows(model, df.loc[indexes], blank.loc[indexes])

        for _, _, _, name, index in events:
            load(name, [index])
            if name == 'Sell':
                legacy_id = prepared['Sell'][1].at[index, 'legacy_id'] \
                    if 'legacy_id' in prepared['Sell'][1].columns else None
                if legacy_id in pinned:
                    load('SellAllocation', pinned[legacy_id])
        if loose:
            load('SellAllocation', loose)

    def check_pinned_allocations(self, prepared):
        """Refuse allocation rows for a sale that would also be allocated automatically."""
        if 'SellAllocation' not in prepared:
            return
        _, allocations, _ = prepared['SellAllocation']
        if 'lookup_legacy_sell' not in allocations.columns:
            return

        strategies = {}
        if 'Sell' in prepared and 'legacy_id' in prepared['Sell'][1].columns:
            sells = prepared['Sell'][1]
            for _, row in sells.iterrows():
                strategies[row['legacy_id']] = row.get('strategy') or SellStrategy.MIN_CGT

        for legacy_id in allocations['lookup_legacy_sell'].dropna().unique():
            strategy = strategies.get(legacy_id)
            if strategy is None:
                strategy = app_models.Sell.objects.filter(
                    account=self.account, legacy_id=legacy_id,
                ).values_list('strategy', flat=True).first()
            if strategy is not None and strategy != SellStrategy.MANUAL:
                raise ValueError(
                    f'The file allocates sale "{legacy_id}" to parcels itself, but that sale\'s '
                    f'strategy is {strategy}, which allocates it automatically as well. Set its '
                    f'strategy to MANUAL.')

    def load_table_to_model(self, model, df):
        df, blank = self.prepare_table(model, df)
        self.load_rows(model, df, blank)

    def prepare_table(self, model, df):
        """The table's columns converted for `model`, and which cells were blank.

        Blank cells are remembered because the blank text defaults below fill them in, and a
        blank cell must not overwrite a stored value when a row is loaded again.
        """
        df = df.copy()
        
        # Legacy data import template has a column 'copy_from_path' which is used to load files.
        # Now, can just use 'file' as the column name, so the export template can be used for importing data also.
        df = df.rename(columns={'copy_from_path': 'file'}, errors='ignore')
        # `_creation_handled` is kept, unlike the audit timestamps beside it. It is not
        # bookkeeping: it is the flag every creation signal checks before deriving anything,
        # so dropping it made a restore derive a second time on top of what the file already
        # held. A file with 271 buys and 291 parcels produced 562 parcels -- one set from
        # the file and one conjured by the signals -- and the cost base adjustments were
        # then allocated across both, which is a wrong cost base rather than a duplicate row.
        #
        # An import template has no such column, so its rows still default to False and the
        # signals still do the deriving. Only a file that already carries the derived rows
        # says so, which is exactly what an export is.
        cols_to_drop = ['created_at', 'updated_at']
        cols_to_drop += [col for col in df.columns if col.startswith('calculated_')]
        df = df.drop(columns=cols_to_drop, errors='ignore')

        if 'account' in [f.name for f in model._meta.fields]:
            if self.account is None:
                raise ValueError(
                    f'{model.__name__} belongs to a portfolio, but none is known yet. The '
                    'file must contain an Account row, and it must load before this table.')
            df['account_id'] = self.account.id

        if 'is_active' in df.columns:
            df['is_active'] = df['is_active'].fillna(True)

        # Normalise pandas null sentinels (NaN, NaT, pd.NA) before field processing
        df = df.astype(object).where(pd.notna(df), None)
        
        logger.debug('Starting to process columns')

        # Preprocess columns to handle foreign keys,  decimal fields, and file fields.
        for col in df.columns:
            logger.debug('Starting to process columns %s', col)
            # Lookup fields are not processed here.
            if col.startswith('lookup_'):
                continue
            
            # Foreign key lookup by name
            col_parts = col.split('__')   # eg 'instrument__name' > ['instrument', 'name']
            if len(col_parts) == 2: 
                base_field_name = col_parts[0]   # instrument
                lookup_field = col_parts[1]  # i.e. name
                field_instance = model._meta.get_field(base_field_name)
                related_model = field_instance.related_model


                df[base_field_name] = df[col].apply(
                    lambda field_val : self.get_related_obj_by_name(
                        related_model=related_model, 
                        account=self.account,
                        filters={lookup_field : field_val}
                        ) if field_val else None
                            )
                df = df.drop(columns=[col])
                continue

            # A column the model no longer has. Renaming or removing a field retires every
            # export taken before it, and since a DataExport is the backup, that turns a
            # backup into a file that cannot be restored -- discovered on the day it is
            # needed. The column is dropped with a warning instead, so an old export still
            # loads and the operator is told which figures it carried that no longer have
            # anywhere to go.
            try:
                field_instance = model._meta.get_field(col)
            except FieldDoesNotExist:
                logger.warning(
                    '%s has no field %r, so that column was ignored. It is probably from an '
                    'export taken before the field was renamed or removed.',
                    model.__name__, col)
                df = df.drop(columns=[col])
                continue

            if isinstance(field_instance, DecimalField):
                df[col] = df[col].apply(lambda v: convert_to_decimal_field(v, field_instance))
            

            elif isinstance(field_instance, FileField):
                df[col] = df[col].apply(process_filefield)

        # Change any NaT, NaN etc to None.
        #
        # `astype(object)` first, exactly as the earlier pass does, and it is not
        # decoration: `DataFrame.where(cond, None)` fills a column that is not already of
        # object dtype with NaN rather than with None, so this line reintroduced the very
        # sentinel it exists to remove. That is what put a float NaN back into a FileField
        # after `process_filefield` had correctly turned it into None, and the save then
        # died asking a float for its `.name`.
        df = df.astype(object).where(pd.notna(df), None)

        df = normalise_cells(df, model)
        blank = df.isnull()
        df = restore_blank_text_defaults(df, model)
        return df, blank

    def load_rows(self, model, df, blank):
        """Create or update one record per row of a table from `prepare_table`."""

        model_has_account = 'account' in [f.name for f in model._meta.fields]

        for index, row in tqdm(df.iterrows(), total=len(df)):

            record = dict(row)
            blank_columns = {column for column in df.columns if blank.at[index, column]}
            if model_has_account:
                record['account_id'] = self.account.id
            id = record.pop('id', None)

            # This is used on loading sell allocations using legacy id.
            lookup_legacy_sell = record.pop('lookup_legacy_sell', None)
            if lookup_legacy_sell:
                sell = self.get_related_obj_by_name(related_model=app_models.Sell, account=self.account, filters={'legacy_id' : lookup_legacy_sell})
                record['sell'] = sell

            # This is used for loading buy allocations using legacy buy id.
            lookup_legacy_buy = record.pop('lookup_legacy_buy', None)

            existing = None
            if id:
                existing = model.objects.filter(id=id).first()
                if existing is not None:
                    self.check_belongs_to_account(obj=existing, model=model)
            else:
                existing = self.get_existing_by_unique_fields(model=model, record=record)

            # Resolving the parcel a pinned allocation names is only meaningful when one is
            # about to be created. Doing it first, for every row, broke loading a file
            # twice: the parcel the row names has by then been consumed by the allocation
            # the first load created, so nothing is available and the import dies -- on a
            # row that only needed updating in place. It survived a partial sale, because
            # the unsold remnant is still a parcel with quantity available, and failed only
            # once a holding was completely sold.
            if lookup_legacy_buy and existing is None:
                record['parcel'] = self.resolve_pinned_parcel(
                    legacy_id=lookup_legacy_buy, model=model, row=row)

            if existing is not None:
                for field, value in record.items():
                    # A blank cell is "no change", not "clear it": the file is usually the
                    # one first loaded, and what was added in the admin since (a document,
                    # notes, a confirmed legal form) is not in it. A blank `file` used to
                    # delete the attached document.
                    if field in blank_columns:
                        continue
                    setattr(existing, field, value)
                context = ('Updating existing object' if id
                           else 'Updating existing object matched on its unique fields')
                save_with_logging(obj=existing, context=context)
                obj = existing
            elif id:
                record['id'] = id  # Preserve provided ID
                obj = model(**record)
                save_with_logging(obj=obj, context="Creating new object with explicitly provided ID")
            else:
                if (not record.get('legacy_id') and model in self._had_rows
                        and self._matched_only_by_legacy_id(model)):
                    raise ValueError(
                        f'{model.__name__} row {index + 1} has no legacy_id, and "{self.account}" '
                        f'already has {model.__name__} records, so it cannot be told apart from '
                        f'them: loading it would add it again rather than update it. Give every '
                        f'{model.__name__} row a legacy_id.')
                fill_blank_currencies(model, record, blank_columns)
                obj = model(**record)
                save_with_logging(obj=obj, context="Creating new object without provided ID")


    def check_belongs_to_account(self, obj, model):
        """Raise ValueError if `obj` already belongs to a different portfolio.

        Loading would otherwise move it out of that portfolio rather than copy it.
        """
        existing_account_id = getattr(obj, 'account_id', None)
        if existing_account_id is None or existing_account_id == self.account.id:
            return

        existing_account = app_models.Account.objects.filter(id=existing_account_id).first()
        raise ValueError(
            f'{model.__name__} {obj.id} already belongs to the portfolio "{existing_account}", so it'
            f' cannot be loaded into "{self.account}". Loading an export into a different portfolio'
            ' would move those records out of the original one rather than copying them. To copy'
            ' them, enter them in an import template (uv run dev make_import_template): an export'
            ' also links its rows to each other by id, so removing its id column is not enough.'
        )


    def get_existing_by_unique_fields(self, model, record):
        """The existing record this one matches, or None.

        Matched on `legacy_id` within the account first, then on any unique constraint whose
        fields are all present. Transactions have no unique constraint, so without a
        `legacy_id` they are always added.
        """
        legacy_id = record.get('legacy_id')
        model_field_names = {field.name for field in model._meta.fields}
        if legacy_id and 'legacy_id' in model_field_names and 'account_id' in record:
            existing = model.objects.filter(account_id=record['account_id'], legacy_id=legacy_id).first()
            if existing is None and str(legacy_id).isdigit():
                # Stored by an earlier load as "1001.0", before numeric ids were read as text.
                # Matched, it is updated to the plain form.
                existing = model.objects.filter(
                    account_id=record['account_id'], legacy_id=f'{legacy_id}.0').first()
            if existing is not None:
                return existing

        for constraint in model._meta.constraints:
            field_names = getattr(constraint, 'fields', None)
            if not field_names:
                continue  # Not a unique constraint over plain fields, so nothing to match on.

            filters = {}
            for field_name in field_names:
                # A foreign key is present in the record either as the object ('instrument') or as
                # the raw id ('account_id'), depending on how the column was processed above.
                attname = model._meta.get_field(field_name).attname
                if attname in record:
                    value = record[attname]
                elif field_name in record:
                    value = record[field_name]
                else:
                    filters = None
                    break
                if value is None:
                    filters = None
                    break
                filters[field_name] = value

            if filters:
                existing = model.objects.filter(**filters).first()
                if existing is not None:
                    return existing

        return None


    def get_or_create_exchange_rate(self, convert_from, exchange_date):
        convert_to = self.account.currency
        if convert_from == convert_to:
            return None
        
        exchange_rate_multiplier = yfinanceinterface.get_exchange_rate(convert_from=convert_from, convert_to=convert_to, exchange_date=exchange_date)
        record = {
            'account' : self.account,
            'date' : date.fromisoformat(str(exchange_date)),
            'convert_from' : convert_from,
            'convert_to' : convert_to,
            'exchange_rate_multiplier' : exchange_rate_multiplier
            }
        exchange_rate, created = app_models.ExchangeRate.objects.get_or_create(**{'convert_from': convert_from, 'convert_to' : convert_to, 'date' : exchange_date}, defaults=record)
        return exchange_rate


    def get_available_parcels(self, legacy_id):
        available_parcels = app_models.Parcel.objects.filter(account=self.account, buy__legacy_id=legacy_id, deactivation_date__isnull=True)
        available_parcels = [parcel for parcel in available_parcels if parcel.remaining_quantity > 0]
        return available_parcels

    def resolve_pinned_parcel(self, legacy_id, model, row):
        """The one available parcel from the buy with `legacy_id`.

        Raises ValueError naming the cause: no such buy, nothing left to allocate, or more
        than one available parcel.
        """
        parcels = self.get_available_parcels(legacy_id=legacy_id)

        if len(parcels) == 1:
            return parcels[0]

        if not parcels:
            if not app_models.Buy.objects.filter(
                    account=self.account, legacy_id=legacy_id).exists():
                raise ValueError(
                    f'{model.__name__} names buy "{legacy_id}", but there is no buy with '
                    f'that legacy id in "{self.account}". Check the spelling, and that the '
                    f'Buy sheet is in the same file.'
                )
            raise ValueError(
                f'{model.__name__} names buy "{legacy_id}", but no parcel from it has any '
                f'quantity left to allocate. Everything bought under that id has already '
                f'been sold, so the allocations in this file would sell it twice.'
            )

        raise ValueError(
            f'{model.__name__} names buy "{legacy_id}", but it has {len(parcels)} parcels '
            f'with quantity available, so which one this row means is ambiguous. A buy '
            f'normally has one; several usually means it was split and the parts were '
            f'partly sold.'
        )


    def get_related_obj_by_name(self, related_model, account, filters):

        if not filters:
            return None

        # Get all field names of the related model
        related_model_fields = {f.name for f in related_model._meta.get_fields()}

        # Add 'account' to filters only if it exists on the related model
        if 'account' in related_model_fields:
            filters['account'] = account

        try:
            return related_model.objects.get(**filters)
        except related_model.DoesNotExist:
            logger.error(f"No match found for {related_model.__name__} with filters: {filters}")
            raise
        except related_model.MultipleObjectsReturned:
            logger.error(f"Multiple matches found for {related_model.__name__} with filters: {filters}")
            raise


    @classmethod
    def clear_all_data(cls):
        res = input("Type 'X' to DELETE ALL DATA.")
        if res.upper() != 'X':
            logger.info('Aborted')
            return
        # Clear database tables

        call_command('flush', interactive=False)

        logger.info('Deleted all models and reset DataLoader state.')
        # Delete all data in the media folder
        media_folder = Path(settings.MEDIA_ROOT)
        force_delete_and_recreate_folder(media_folder)
        

def force_delete_and_recreate_folder(folder_path):
    folder = Path(folder_path)
    # Check if folder exists
    if folder.exists():
        for item in folder.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()  # Force delete files
            except Exception as e:
                logger.error(f"Failed to delete {item}: {e}", exc_info=True)
    # Recreate folder
    folder.mkdir(parents=True, exist_ok=True)
    logger.info(f"Forcefully deleted and recreated folder: {folder}")





from share_dinkum_app.models import Account, DataExport

class DataBackupManager:
    """Backup and restore, with the parts that need Django.

    Copying, layout and pruning are in `share_dinkum_app.backup`. This adds the Excel
    exports bundled with a backup, and restoring, which takes a pre-restore copy first.
    """

    BACKUP_FOLDER_FORMAT = backup_module.BACKUP_FOLDER_FORMAT
    RETAIN_BACKUPS = backup_module.RETAIN_BACKUPS

    # Restores copy the live data here first, so an unwanted restore can be undone.
    PRE_RESTORE_NAME = 'pre_restore'
    RETAIN_PRE_RESTORE = 3

    def __init__(self, base_path: Path = None):
        #: Defaults to the shared backup root; tests pass their own.
        self.base_path = Path(base_path) if base_path else backup_module.DEFAULT_BACKUP_ROOT

    def list_backups(self, name):
        """Backup folder names within a set, newest first."""
        return backup_module.list_backups(self.base_path, name)

    @staticmethod
    def copy_sqlite_database(source: Path, destination: Path):
        """Alias for `backup.copy_sqlite_database`."""
        backup_module.copy_sqlite_database(source, destination)

    def create_data_exports_for_all_accounts(self, include_price_history: bool = True):
        """Create a DataExport for every account; a signal writes each file."""
        accounts = Account.objects.all()
        logger.info(f"Creating DataExport for {accounts.count()} accounts")
        with transaction.atomic():
            for account in accounts:
                export = DataExport.objects.create(
                    account=account,
                    include_price_history=include_price_history
                )
                export.refresh_from_db()


    def cleanup_old_backups(self, name, keep=None):
        """Keep only the most recent `keep` backups in a set."""
        removed = backup_module.cleanup_old_backups(
            self.base_path, name, keep or self.RETAIN_BACKUPS)
        for folder_name in removed:
            logger.info('Deleted old backup: %s', folder_name)

    def backup(self, name=backup_module.DEFAULT_NAME, include_data_export=True):
        """Copy the database and media into a new backup, pruning older ones.

        `include_data_export` first writes an Excel export per portfolio into media, so the
        backup includes a readable copy. Returns the `make_backup` result, or None if there
        is no data.
        """
        if include_data_export:
            self.create_data_exports_for_all_accounts()

        result = backup_module.make_backup(
            database=Path(settings.DATABASES['default']['NAME']),
            media=Path(settings.MEDIA_ROOT),
            root=self.base_path,
            name=name,
            keep=self.RETAIN_BACKUPS,
        )
        if result is None:
            logger.info('No data to back up yet.')
            return None

        logger.info('Backup completed successfully at %s', result['path'])
        return result

    def restore(self, name):
        """Interactively restore the database and media from one of a set's five latest backups.

        Asks which backup and for confirmation, then takes a pre-restore copy first.
        """

        backup_base_path = self.base_path / name

        # The menu and the selection must index the same list, or the restore loads a different
        # backup from the one shown.
        recent_backups = self.list_backups(name)[:5]
        if not recent_backups:
            logger.error(f"No backups found in {backup_base_path}")
            return

        backup_choice_text = "\n".join(
            f"{i + 1}. {backup}{'   (latest)' if i == 0 else ''}"
            for i, backup in enumerate(recent_backups)
        )

        choice = input(f"Available backups:\n{backup_choice_text}\nSelect a backup to restore (1-{len(recent_backups)}). Type '1' to choose the latest backup.\n:")
        try:
            choice_index = int(choice) - 1
            if choice_index < 0 or choice_index >= len(recent_backups):
                raise ValueError("Choice out of range")
            selected_backup = recent_backups[choice_index]
        except Exception as e:
            logger.error(f"Invalid choice. Restore cancelled.")
            return

        backup_path = backup_base_path / selected_backup

        db_file = Path(settings.DATABASES['default']['NAME'])
        backup_db_file = backup_path / db_file.name
        media_backup = backup_path / "media"

        # Validate before asking to confirm, so an unusable backup cannot destroy the live data.
        if not backup_db_file.exists() or not media_backup.exists():
            raise FileNotFoundError(f"Backup {selected_backup} is incomplete or missing files")

        res = input(f"Type 'X' to OVERWRITE current data with the backup taken at {selected_backup}.")
        if res.upper() != 'X':
            logger.info("Restore cancelled.")
            return

        logger.info(f"Restoring from backup: {backup_path}")

        self.snapshot_current_data()

        # Close DB connections
        connections.close_all()

        # Restore DB
        logger.info(f"Restoring SQLite DB from {backup_db_file} to {db_file}")
        shutil.copy2(backup_db_file, db_file)

        # Restore media
        if Path(settings.MEDIA_ROOT).exists():
            shutil.rmtree(Path(settings.MEDIA_ROOT))
        shutil.copytree(media_backup, Path(settings.MEDIA_ROOT))

        logger.info("Restore completed successfully.")


    def snapshot_current_data(self):
        """Copy the live database and media to the pre-restore set, so a restore can be undone.

        Writes nothing to the database. Undo with `restore(name=PRE_RESTORE_NAME)`.
        """
        folder_name = datetime.now().strftime(self.BACKUP_FOLDER_FORMAT)
        snapshot_path = self.base_path / self.PRE_RESTORE_NAME / folder_name
        snapshot_path.mkdir(parents=True, exist_ok=True)

        db_file = Path(settings.DATABASES['default']['NAME'])
        if db_file.exists():
            self.copy_sqlite_database(db_file, snapshot_path / db_file.name)

        media_root = Path(settings.MEDIA_ROOT)
        if media_root.exists():
            shutil.copytree(media_root, snapshot_path / 'media')

        self.cleanup_old_backups(name=self.PRE_RESTORE_NAME, keep=self.RETAIN_PRE_RESTORE)

        logger.info(f"Current data saved to {snapshot_path} before restoring.")
        return snapshot_path