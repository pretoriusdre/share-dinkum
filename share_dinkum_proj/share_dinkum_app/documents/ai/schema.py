"""The shape the AI must reply in, built from the same columns the parsers fill, and reading it back.

Money, units and dates come back as text, so no figure passes through a float on the way.
"""

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from share_dinkum_app.documents import columns
from share_dinkum_app.documents.extracted import Extracted

NULLABLE_TEXT = {'type': ['string', 'null']}


class ReplyError(ValueError):
    """The reply is not JSON in the required shape."""


def output_schema() -> dict[str, Any]:
    """A JSON Schema for `{"events": [...]}`, one shape per model.

    Every property is required and may be null, which is what strict structured output asks for.
    """
    variants = []
    for model in columns.MODELS:
        row = {column: NULLABLE_TEXT for column in columns.columns(model)}
        variants.append({
            'type': 'object',
            'properties': {
                'model': {'type': 'string', 'enum': [model]},
                'reference': NULLABLE_TEXT,
                'stated_total': NULLABLE_TEXT,
                'row': {'type': 'object', 'properties': row, 'required': list(row), 'additionalProperties': False},
            },
            'required': ['model', 'reference', 'stated_total', 'row'],
            'additionalProperties': False,
        })
    return {
        'type': 'object',
        'properties': {'events': {'type': 'array', 'items': {'anyOf': variants}}},
        'required': ['events'],
        'additionalProperties': False,
    }


def parse_reply(text: str) -> list[Extracted]:
    """The events in a reply. Raises ReplyError naming everything wrong with it."""
    cleaned = re.sub(r'^\s*```(?:json)?\s*|\s*```\s*$', '', text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise ReplyError(f'The reply is not JSON: {error}.') from None
    if not isinstance(data, dict) or not isinstance(data.get('events'), list):
        raise ReplyError('The reply must be an object with an "events" list.')

    events: list[Extracted] = []
    problems: list[str] = []
    for number, item in enumerate(data['events'], start=1):
        try:
            events.append(_event(item))
        except ValueError as error:
            problems.append(f'Event {number}: {error}')
    if problems:
        raise ReplyError(' '.join(problems))
    return events


def _event(item: Any) -> Extracted:
    if not isinstance(item, dict) or not isinstance(item.get('row'), dict):
        raise ValueError('it must be an object with a "row" object.')
    model = item.get('model')
    if model not in columns.MODELS:
        raise ValueError(f'model {model!r} is not one of {", ".join(columns.MODELS)}.')
    known = set(columns.columns(model))
    row = {column: columns.from_json(model, column, value) if column in known else value
           for column, value in item['row'].items() if value is not None and value != ''}
    stated = item.get('stated_total')
    try:
        stated_total = Decimal(str(stated).replace(',', '')) if stated not in (None, '') else None
    except InvalidOperation:
        raise ValueError(f'stated_total is not a number: {stated!r}.') from None
    reference = item.get('reference')
    return Extracted(model=model, row=row, reference=str(reference) if reference else None,
                     stated_total=stated_total)
