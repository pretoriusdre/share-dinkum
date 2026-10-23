"""Computershare dividend advices for CDIs over a US company's stock, paid in US dollars with US tax
withheld, from 2013 on, one dividend a page."""

import re
from decimal import Decimal

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'computershare_cdi_payment'
KIND = 'text'

# `CDI US$0.025 1,740 US$43.50`. OCR has read `CDI` as `CDt`.
ROW = re.compile(r'^CD\S[ \t]+US\$(\d+(?:\.\d+)?)[ \t]+(\d[\d,]*)[ \t]+US\$(\d[\d,]*\.\d{2})[ \t]*$', re.MULTILINE)


def recognises(doc: Document) -> bool:
    return _common.has_all(doc.text or '', 'Computershare', 'per fully paid CDI', 'Converted Net Payment')


def parse(doc: Document) -> list[Extracted]:
    # An older file can hold several advices, a page each.
    return [_dividend(page) for page in (doc.text or '').split('\f') if _common.has_all(page, 'Converted Net Payment')]


def _dividend(text: str) -> Extracted:
    row = ROW.search(text)
    if row is None:
        raise _common.ParseError('No row of US$ rate, securities and US$ total found after "CDI".')
    withheld = _common.labelled(text, 'Non-Resident Withholding Tax', r'US\$\d[\d,]*\.\d{2}')
    return Extracted('Dividend', {
        'instrument__name': _common.labelled(text, 'ASX Code', _common.CODE) if 'ASX Code' in text else _mailing_code(text),
        'date': _common.date(_common.labelled(text, 'Payment Date', _common.DATE)),
        'quantity': _common.units(row.group(2)),
        'dividend_type': 'FOREIGN',
        'unfranked_amount_per_share': _common.money(row.group(1)),
        'unfranked_amount_per_share_currency': 'USD',
        'franked_amount_per_share': Decimal(0),
        'franked_amount_per_share_currency': 'USD',
        # US tax withheld, which an Australian resident claims as a foreign income tax offset.
        'foreign_tax_credit': _common.money(withheld),
        'foreign_tax_credit_currency': 'USD',
    }, reference=_common.labelled(text, 'Direct Credit Reference No.', r'\d+'),
        stated_total=_common.money(row.group(3)))


def _mailing_code(text: str) -> str:
    """Later advices print the code only in the mailing line: `T 031470 000 RMD`."""
    match = re.search(rf'^T \d+ 000 ({_common.CODE})\b', text, re.MULTILINE)
    if match is None:
        raise _common.ParseError('No value found after "ASX Code".')
    return match.group(1)
