"""Help text for the columns of an Excel table, shown as a note on each header cell.

The text comes from the model fields' `help_text`. Columns that are not plain fields get theirs
from here: a `__name` lookup takes its relation's, a currency column is described from its
amount, and the few template-only columns are written out below. A template header also says
whether the column is required, what a blank means, and the values a choice column accepts.
"""

from collections.abc import Iterable
from typing import Any

from django.db import models
from django.db.models import Field
from djmoney.models.fields import CurrencyField

import share_dinkum_app.models as app_models

APP_NOTE = 'Set by the app; do not edit.'

# Django's own user fields carry no help text of their own.
AUTH_FIELD_HELP = {
    'password': 'Hashed password. Never exported.',
    'last_login': 'When the user last signed in.',
    'first_name': 'First name.',
    'last_name': 'Last name.',
    'email': 'Email address.',
    'date_joined': 'When the user was created.',
}

# Columns of an import template that are not model fields, by table, then column.
EXTRA_COLUMN_HELP: dict[str, dict[str, str]] = {
    'SellAllocation': {
        'lookup_legacy_sell': 'The legacy_id of the Sell row this allocation belongs to.',
        'lookup_legacy_buy': ('The legacy_id of the Buy row whose parcel the units are taken from. The buy must have '
                              'one parcel with units left.'),
    },
    'AttributionStatement': {
        'lookup_legacy_adjustment': ('The legacy_id of the CostBaseAdjustment entered from this same statement. '
                                     'Optional; used to check the two agree.'),
    },
    'AttributionComponent': {
        'lookup_legacy_statement': 'The legacy_id of the AttributionStatement row this line is from.',
    },
    'CapitalLossCarryForward': {
        'financial_year_end_date': ('Any date in the financial year the loss was made in, usually its last day. '
                                    'Not the year it is used in.'),
    },
}

# How many choices to list on a header before saying there are more.
MAX_LISTED_CHOICES = 12


def field_help(field: 'Field[Any, Any]') -> str | None:
    """The field's own help text, or what can be said of it where it has none."""
    if field.help_text:
        return str(field.help_text)

    if isinstance(field, CurrencyField):
        amount = getattr(field, 'price_field', None)
        amount_name = amount.name if amount is not None else field.name.removesuffix('_currency')
        text = f'Currency of {amount_name}, as a code such as AUD or USD.'
        if amount_name.startswith('calculated_'):
            return f'{text} {APP_NOTE}'
        return text

    if field.model is app_models.AppUser:
        return AUTH_FIELD_HELP.get(field.name)
    return None


def _choices_text(field: 'Field[Any, Any]') -> str | None:
    if isinstance(field, CurrencyField) or not field.choices:
        return None
    choices = list(field.choices)
    listed = '; '.join(f'{key} ({label})' if str(key) != str(label) else f'{key}' for key, label in choices[:MAX_LISTED_CHOICES])
    more = f'; and {len(choices) - MAX_LISTED_CHOICES} more' if len(choices) > MAX_LISTED_CHOICES else ''
    return f'One of: {listed}{more}.'


def _requirement_text(field: 'Field[Any, Any]') -> str:
    """Whether a template cell must be filled, and what leaving it blank does."""
    if isinstance(field, CurrencyField) and any(f.name == 'instrument' for f in field.model._meta.fields):
        # fill_blank_currencies: a new record's blank currency takes the instrument's.
        return "Optional. Blank = the instrument's currency."
    if field.has_default() and not field.null:
        default = field.get_default()
        shown = 'False' if default is False else 'True' if default is True else str(default)
        return f'Optional. Blank = {shown}.' if shown not in ('', 'None') else 'Optional.'
    if field.null or field.blank:
        return 'Optional.'
    return 'Required.'


def _template_text(field: 'Field[Any, Any]') -> list[str]:
    parts = []
    if isinstance(field, models.DateField) and not isinstance(field, models.DateTimeField):
        parts.append('A date, such as 2024-07-01.')
    choices = _choices_text(field)
    if choices:
        parts.append(choices)
    parts.append(_requirement_text(field))
    return parts


def describe_columns(model: type[models.Model], columns: Iterable[str], template: bool = False) -> dict[str, str]:
    """Header-note text for each of `columns` that can be described, keyed by column name.

    `template` adds what a person filling the table in needs: required or optional, the default a
    blank takes, and the allowed values. A column nothing is known about is left out.
    """
    names = {field.name: field for field in model._meta.fields}
    extras = EXTRA_COLUMN_HELP.get(model.__name__, {})
    described: dict[str, str] = {}

    for column in columns:
        if column in extras:
            described[column] = extras[column]
            continue

        field = names.get(column)
        suffix = ''
        if field is None and '__' in column:
            # `instrument__name`: the related record's name, written in place of the record.
            base = column.split('__')[0]
            field = names.get(base)
        elif field is None and column.endswith('_id'):
            field = names.get(column.removesuffix('_id'))
            suffix = ' The id of the record.'
        if field is None:
            continue

        text = field_help(field)
        if text is None:
            continue
        parts = [text + suffix]
        if template:
            parts += _template_text(field)
        described[column] = '\n'.join(parts) if template else parts[0]

    return described
