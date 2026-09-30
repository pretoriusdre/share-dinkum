"""Build an empty import template (headers only) from the models, for a new portfolio.

Generated from the models so the headers always match what the loader expects.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db.models import Model

from djmoney.models.fields import MoneyField

from share_dinkum_app import excelinterface
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
]

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
EXTRA_COLUMNS: dict[type[Model], list[str]] = {
    app_models.SellAllocation: ['lookup_legacy_sell', 'lookup_legacy_buy'],
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

    # legacy_id identifies the row within the file, so it reads best first, and notes last.
    ordered = [column for column in columns if column == 'legacy_id']
    ordered += [column for column in columns if column not in ('legacy_id', 'notes')]
    ordered += [column for column in columns if column == 'notes']
    return ordered


def build_template(output_path: Path) -> None:
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
        df = pd.DataFrame([{column: None for column in columns}])

        generator.add_table(
            df=df,
            # The loader finds each table by the model's own name, so this has to match exactly.
            table_name=model.__name__,
            description=getattr(model, 'MODEL_DESCRIPTION', None),
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
