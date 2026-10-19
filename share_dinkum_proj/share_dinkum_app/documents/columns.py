"""The columns an event from a document may fill, per model, and what each holds.

They are the import template's columns (`make_import_template.get_template_columns`), less those the
app sets itself or that are a choice of the user's rather than a fact the document states.
"""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from django.apps import apps
from django.db import models
from djmoney.models.fields import CurrencyField, MoneyField

from share_dinkum_app import column_help

# The models a document's events load into.
MODELS = ('Buy', 'Sell', 'Dividend', 'Distribution', 'AttributionStatement', 'AttributionComponent',
          'CostBaseAdjustment')

# Set by the app: `legacy_id` and `file` when the event is loaded, the statement link from the order
# of the events. `description` and `notes` are the user's own words.
APP_COLUMNS = {'legacy_id', 'file', 'notes', 'description'}
# The user's choice, not something the document says.
USER_CHOICE_COLUMNS = {'strategy', 'allocation_method'}

# The columns every event of the model must fill.
REQUIRED: dict[str, tuple[str, ...]] = {
    'Buy': ('instrument__name', 'date', 'quantity', 'unit_price', 'total_brokerage'),
    'Sell': ('instrument__name', 'date', 'quantity', 'unit_price', 'total_brokerage'),
    'Dividend': ('instrument__name', 'date', 'quantity', 'franked_amount_per_share', 'unfranked_amount_per_share'),
    'Distribution': ('instrument__name', 'date', 'quantity', 'distribution_amount_per_share'),
    'AttributionStatement': ('instrument__name', 'financial_year_end_date'),
    'AttributionComponent': ('component', 'amount'),
    'CostBaseAdjustment': ('instrument__name', 'financial_year_end_date', 'cost_base_increase'),
}

# A per-unit figure is compared exactly; a whole amount to the cent.
PER_UNIT_COLUMNS = {'quantity', 'unit_price', 'distribution_amount_per_share', 'franked_amount_per_share',
                    'unfranked_amount_per_share'}

ValueKind = Literal['decimal', 'date', 'currency', 'choice', 'text', 'instrument']


def model_class(name: str) -> type[models.Model]:
    """The model `name` (one of `MODELS`) loads into."""
    if name not in MODELS:
        raise ValueError(f'{name!r} is not a model a document can give. It is one of: {", ".join(MODELS)}.')
    return apps.get_model('share_dinkum_app', name)


def columns(model: str) -> list[str]:
    """The columns an event of `model` may fill, in template order."""
    from share_dinkum_app.management.commands.make_import_template import get_template_columns

    return [column for column in get_template_columns(model_class(model))
            if column not in APP_COLUMNS | USER_CHOICE_COLUMNS and not column.startswith('lookup_legacy')]


def field_for(model: str, column: str) -> 'models.Field[Any, Any]':
    """The model field behind `column`: the relation for `instrument__name`."""
    return model_class(model)._meta.get_field(column.split('__')[0])  # type: ignore[return-value]


def kind(model: str, column: str) -> ValueKind:
    """What a value of `column` is."""
    if column == 'instrument__name':
        return 'instrument'
    field = field_for(model, column)
    if isinstance(field, CurrencyField):
        return 'currency'
    if isinstance(field, (MoneyField, models.DecimalField)):
        return 'decimal'
    if isinstance(field, models.DateField):
        return 'date'
    if field.choices:
        return 'choice'
    return 'text'


def choices(model: str, column: str) -> list[str]:
    """The values a choice column allows."""
    return [str(key) for key, _ in field_for(model, column).choices or []]


def problem_with_value(model: str, column: str, value: Any) -> str | None:
    """Why `value` is not right for `column`, or None."""
    expected = kind(model, column)
    if expected == 'decimal' and not isinstance(value, Decimal):
        return f'{column} must be a Decimal, not {type(value).__name__} ({value!r}).'
    if expected == 'date' and not (isinstance(value, date) and type(value) is date):
        return f'{column} must be a datetime.date, not {type(value).__name__} ({value!r}).'
    if expected == 'currency' and not (isinstance(value, str) and len(value) == 3 and value.isupper()):
        return f'{column} must be a currency code such as AUD, not {value!r}.'
    if expected == 'choice' and str(value) not in choices(model, column):
        return f'{column} must be one of {", ".join(choices(model, column))}, not {value!r}.'
    if expected in ('text', 'instrument') and not isinstance(value, str):
        return f'{column} must be text, not {type(value).__name__} ({value!r}).'
    return None


def from_json(model: str, column: str, value: Any) -> Any:
    """A value written as JSON (text for money and dates) turned into what a row holds.

    Raises ValueError for a value that cannot be one.
    """
    expected = kind(model, column)
    if expected == 'decimal':
        try:
            return Decimal(str(value).replace(',', '').strip())
        except InvalidOperation:
            raise ValueError(f'{column} is not a number: {value!r}.') from None
    if expected == 'date':
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError:
            raise ValueError(f'{column} is not a date written as YYYY-MM-DD: {value!r}.') from None
    return str(value).strip()


def to_json(value: Any) -> Any:
    """A row value written as JSON: a Decimal as `str(Decimal)`, a date as ISO, text as it is."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return value


def describe(model: str) -> list[tuple[str, str, str]]:
    """(column, required or optional, meaning) for each column of `model`."""
    notes = column_help.describe_columns(model_class(model), columns(model))
    described = []
    for column in columns(model):
        meaning = notes.get(column, '')
        if kind(model, column) == 'choice':
            meaning = f'{meaning} One of: {", ".join(choices(model, column))}.'.strip()
        required = 'required' if column in REQUIRED[model] else 'optional'
        described.append((column, required, meaning))
    return described


def markdown() -> str:
    """Every model's columns as Markdown tables."""
    parts = []
    for model in MODELS:
        description = getattr(model_class(model), 'MODEL_DESCRIPTION', '')
        lines = [f'### {model}', '', description, '', '| Column | Required | Meaning |', '|---|---|---|']
        lines += [f'| `{column}` | {required} | {meaning} |' for column, required, meaning in describe(model)]
        parts.append('\n'.join(lines))
    return '\n\n'.join(parts)
