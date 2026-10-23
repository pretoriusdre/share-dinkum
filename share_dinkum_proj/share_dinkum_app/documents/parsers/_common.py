"""Helpers every parser uses, so each layout's file holds only what is particular to that layout."""

import re
from collections.abc import Sequence
from datetime import date as Date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import pandas as pd

# A printed amount: `1,234.56`, `$1,234.56`, `-12.5`, `(12.50)`, `0.4367`.
NUMBER = r'\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\)?|\(?-?\$?\s?\.\d+\)?'
# A printed date: `16/07/2025`, `16-07-25`, `16 Jul 2025`, `16 July 2025`, `16-Jul-2025`, `2025-07-16`.
DATE = r'\d{4}-\d{2}-\d{2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|\d{1,2}(?:st|nd|rd|th)?[ -][A-Za-z]{3,9},?[ -]\d{2,4}'
# An exchange code: `VAS`, `BHP`, `A200`.
CODE = r'[A-Z0-9]{2,6}'

DATE_FORMATS = ('%Y-%m-%d', '%d/%m/%Y', '%d/%m/%y', '%d-%m-%Y', '%d-%m-%y', '%d.%m.%Y', '%d %b %Y', '%d %B %Y',
                '%d-%b-%Y', '%d-%b-%y', '%d %b %y', '%d %B, %Y', '%d %b, %Y')


class ParseError(ValueError):
    """A document of this layout lacks a value the parser needs. Say which label was looked for."""


def money(text: str) -> Decimal:
    """A printed amount as a Decimal, every printed decimal place kept.

    Drops `$`, `A$`, `US$`, a currency code, commas and spaces. `(12.50)`, `-12.50` and `12.50-` are
    negative.
    """
    cleaned = re.sub(r'\b(?:AUD|USD|NZD|GBP|EUR)\b|[A-Z]{0,2}\$|,|\s', '', text)
    negative = cleaned.startswith('(') and cleaned.endswith(')') or cleaned.startswith('-') or cleaned.endswith('-')
    cleaned = cleaned.strip('()-')
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        raise ParseError(f'{text!r} is not an amount.') from None
    return -value if negative else value


def units(text: str) -> Decimal:
    """A printed number of units as a Decimal: `1,000` or `1000.5`."""
    try:
        return Decimal(text.replace(',', '').strip())
    except InvalidOperation:
        raise ParseError(f'{text!r} is not a number of units.') from None


def cents_to_dollars(cents: Decimal) -> Decimal:
    """A rate printed in cents, in dollars, exactly: `Decimal('143.6788')` gives `Decimal('1.436788')`."""
    return cents.scaleb(-2)


def date(text: str) -> Date:
    """A printed date. Day before month, as Australian documents print it. `Sept` is read as `Sep`."""
    cleaned = re.sub(r'(\d)(?:st|nd|rd|th)\b', r'\1', ' '.join(text.split()))
    cleaned = re.sub(r'\bSept\b', 'Sep', cleaned, flags=re.IGNORECASE)
    for date_format in DATE_FORMATS:
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    raise ParseError(f'{text!r} is not a date.')


# The time zone a trade on each exchange is dated in, by the exchange's code as brokers print it.
EXCHANGE_ZONES = {'ASX': 'Australia/Sydney', 'LSE': 'Europe/London', 'LSEETF': 'Europe/London',
                  'NASDAQ': 'America/New_York', 'NYSE': 'America/New_York', 'ARCA': 'America/New_York',
                  'BATS': 'America/New_York', 'SGX': 'Asia/Singapore'}


def exchange_date(when: datetime, zone: str, exchange: str) -> Date:
    """The date on `exchange` of `when`, a time printed in the time zone `zone` (`America/New_York`).

    A broker abroad prints a trade's time in its own zone: an ASX trade at 22:11 New York time was made the
    next morning in Sydney, and that is its date.
    """
    if exchange not in EXCHANGE_ZONES:
        raise ParseError(f'No time zone is known for the exchange {exchange!r}.')
    return when.replace(tzinfo=ZoneInfo(zone)).astimezone(ZoneInfo(EXCHANGE_ZONES[exchange])).date()


def _label_pattern(label: str) -> str:
    return r'\s+'.join(re.escape(word) for word in label.split())


def labelled_or_none(text: str, label: str, pattern: str = NUMBER) -> str | None:
    """The first value matching `pattern` after `label` on the same line, or None.

    The label is matched word for word, ignoring case and spacing, and may be followed by a colon.
    """
    match = re.search(rf'{_label_pattern(label)}\s*:?[ \t]*({pattern})', text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def labelled(text: str, label: str, pattern: str = NUMBER) -> str:
    """As `labelled_or_none`, raising `ParseError` naming the label when there is no value."""
    value = labelled_or_none(text, label, pattern)
    if value is None:
        raise ParseError(f'No value found after "{label}".')
    return value


def has_all(text: str, *phrases: str) -> bool:
    """Whether `text` has every phrase in it, ignoring spacing and line breaks, but not case.

    A missing space counts as spacing too, since OCR sometimes drops one.
    """
    flat = ''.join(text.split())
    return all(''.join(phrase.split()) in flat for phrase in phrases)


def header_is(table: pd.DataFrame, header: Sequence[str]) -> bool:
    """Whether the table's columns are exactly `header`, in order."""
    return [str(column).strip() for column in table.columns] == list(header)
