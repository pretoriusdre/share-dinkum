"""Build an empty import template (headers only) from the models, for a new portfolio.

Generated from the models so the headers always match what the loader expects.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Model

from djmoney.models.fields import CurrencyField, MoneyField

from share_dinkum_app import column_help, excelinterface
import share_dinkum_app.models as app_models


# The tables a person fills in. Everything else is either derived by the app (Parcel,
# SellAllocation's own parcel link, fiscal years) or shared setup that already exists.
TEMPLATE_MODELS = [
    app_models.Market,
    app_models.Instrument,
    app_models.Buy,
    app_models.Sell,
    app_models.SellAllocation,
    app_models.ShareSplit,
    app_models.CostBaseAdjustment,
    app_models.Dividend,
    app_models.Distribution,
    app_models.ResidencyPeriod,
    app_models.InstrumentValuation,
    app_models.CapitalLossCarryForward,
    app_models.AttributionStatement,
    app_models.AttributionComponent,
]

# Tables a portfolio works without. The app only needs them for the tax reports that use them, and
# each can be entered in the app later, so they are marked as optional on the index sheet and given
# a grey tab. An empty table loads nothing.
OPTIONAL_MODELS: set[type[Model]] = {
    app_models.ResidencyPeriod,
    app_models.InstrumentValuation,
    app_models.CapitalLossCarryForward,
    app_models.AttributionStatement,
    app_models.AttributionComponent,
}
OPTIONAL_NOTE = 'Optional: you can leave this table empty, or enter this information in the app later.'
OPTIONAL_TAB_COLOR = 'A6A6A6'

# Set by the app rather than by the person filling in the file. 'account' is the one being loaded
# into, 'exchange_rate' is looked up from the transaction date on save, and 'current_unit_price' is
# refreshed from market data, so anything typed into it would be overwritten.
EXCLUDED_FIELDS = {
    'id',
    'account',
    'created_at',
    'updated_at',
    'is_active',
    'exchange_rate',
    'current_unit_price',
    '_creation_handled',
}

# Columns which are not plain model fields. SellAllocation points at a Parcel, which does not exist
# until the buy it came from has been loaded, so the file refers to the buy and the sell by the
# legacy_id given to them in this same file. See DataLoader.load_table_to_model.
#
# An attribution statement is matched to the cost base adjustment from the same annual statement by
# that adjustment's legacy_id, and each of its components to the statement by the statement's.
EXTRA_COLUMNS: dict[type[Model], list[str]] = {
    app_models.SellAllocation: ['lookup_legacy_sell', 'lookup_legacy_buy'],
    app_models.AttributionStatement: ['lookup_legacy_adjustment'],
    app_models.AttributionComponent: ['lookup_legacy_statement'],
}

# A fiscal year is a shared table whose rows are only created as dates are classified, so a person
# cannot name one. They give the date the year ends on, as they do for a cost base adjustment.
COLUMN_SUBSTITUTIONS: dict[type[Model], dict[str, str]] = {
    app_models.CapitalLossCarryForward: {'fiscal_year__name': 'financial_year_end_date'},
}


def get_lookup_column(field: Any) -> str | None:
    """The `field__name` or `field__code` lookup column for a relation, or None if neither exists."""
    related_field_names = {f.name for f in field.related_model._meta.fields}
    for candidate in ('name', 'code'):
        if candidate in related_field_names:
            return f'{field.name}__{candidate}'
    return None


def get_template_columns(model: type[Model]) -> list[str]:
    """A model's fillable columns: legacy_id first, notes last, currency after its amount."""
    field_names = {f.name for f in model._meta.fields}
    money_field_names = {f.name for f in model._meta.fields if isinstance(f, MoneyField)}

    columns: list[str] = []
    for field in model._meta.fields:
        name = field.name

        if name in EXCLUDED_FIELDS or name.startswith('calculated_'):
            continue

        # 'description' is editable on the reference tables but built by the app on transactions.
        # legacy_id is not editable either, but the loader accepts it and the template needs it.
        if not field.editable and name != 'legacy_id' and not name.endswith('_currency'):
            continue

        # A MoneyField comes with its own currency field. Keep them together, money first.
        if name.endswith('_currency') and name[: -len('_currency')] in money_field_names:
            continue

        if field.is_relation:
            lookup_column = get_lookup_column(field)
            if lookup_column:
                columns.append(lookup_column)
            continue

        columns.append(name)
        currency_column = f'{name}_currency'
        if name in money_field_names and currency_column in field_names:
            columns.append(currency_column)

    columns.extend(EXTRA_COLUMNS.get(model, []))
    substitutions = COLUMN_SUBSTITUTIONS.get(model, {})
    columns = [substitutions.get(column, column) for column in columns]

    # legacy_id identifies the row within the file, so it reads best first, and notes last.
    ordered = [column for column in columns if column == 'legacy_id']
    ordered += [column for column in columns if column not in ('legacy_id', 'notes')]
    ordered += [column for column in columns if column == 'notes']
    return ordered


def get_dropdowns(model: type[Model], columns: list[str]) -> dict[str, list[str] | tuple[str, list[str]]]:
    """The columns with a fixed set of values, each with its list name and the allowed keys.

    Every currency column shares one list. Lookups such as `instrument__name` are not here: they
    name a record, and one loaded in an earlier file is a legitimate answer.
    """
    fields = {field.name: field for field in model._meta.fields}
    dropdowns: dict[str, list[str] | tuple[str, list[str]]] = {}
    for column in columns:
        field = fields.get(column)
        if field is None or not field.choices:
            continue
        list_name = 'currency' if isinstance(field, CurrencyField) else f'{model.__name__}.{column}'
        dropdowns[column] = (list_name, [str(key) for key, _label in field.choices])
    return dropdowns


def get_table_description(model: type[Model]) -> str | None:
    """The model's description for the index sheet, saying so where the table is optional."""
    description: str | None = getattr(model, 'MODEL_DESCRIPTION', None)
    if model in OPTIONAL_MODELS:
        return f'{OPTIONAL_NOTE} {description}' if description else OPTIONAL_NOTE
    return description


def build_template(output_path: Path, data: dict[str, pd.DataFrame] | None = None) -> None:
    """Write the template. `data` maps a model name to rows to fill its table with, as a frame.

    Without `data` every table is empty. A frame's columns must all be ones the table has, so a
    column that was misspelt, or that the template no longer offers, is an error and not lost.
    """
    generator = excelinterface.ExcelGen(
        title='Share Dinkum import template',
        description='Fill in one file per portfolio. Loading a file only ever adds to the portfolio it is loaded into.',
        url='https://github.com/pretoriusdre/share-dinkum',
    )

    for model in TEMPLATE_MODELS:
        columns = get_template_columns(model)

        # Excel will not accept a table whose range is nothing but its header row, so each table
        # gets one blank row. get_all_tables_in_excel drops all-empty rows, so it reads back as no
        # records at all.
        filled = (data or {}).get(model.__name__)
        if filled is not None and not filled.empty:
            unknown = [column for column in filled.columns if column not in columns]
            if unknown:
                raise ValueError(f'{model.__name__} has no column {unknown}. Its columns are {columns}.')
            df = filled.reindex(columns=columns).astype(object)
            df = df.where(df.notna(), None)
        else:
            df = pd.DataFrame([{column: None for column in columns}])

        generator.add_table(
            df=df,
            # The loader finds each table by the model's own name, so this has to match exactly.
            table_name=model.__name__,
            description=get_table_description(model),
            tab_color=OPTIONAL_TAB_COLOR if model in OPTIONAL_MODELS else None,
            column_descriptions=column_help.describe_columns(model, columns, template=True),
            dropdowns=get_dropdowns(model, columns),
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    generator.save(output_path)


class Command(BaseCommand):
    help = 'Create an empty Excel import template, ready to fill in for one portfolio.'

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            '--output',
            default=None,
            help='Where to write the template. Defaults to share_dinkum_app/import_data/data_import_template_blank.xlsx',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='Overwrite the file if it already exists.',
        )

    def handle(self, *args: Any, **options: Any) -> None:
        default_path = Path(__file__).resolve().parents[2] / 'import_data' / 'data_import_template_blank.xlsx'
        output_path = Path(options['output']).resolve() if options['output'] else default_path

        if output_path.exists() and not options['force']:
            raise CommandError(
                f'{output_path} already exists. Pass --force to overwrite it, or --output to write somewhere else.'
            )

        build_template(output_path)

        self.stdout.write(self.style.SUCCESS(f'Wrote an empty import template to {output_path}'))
        self.stdout.write('Take a copy per portfolio, fill it in, then load it from data_import.ipynb.')
