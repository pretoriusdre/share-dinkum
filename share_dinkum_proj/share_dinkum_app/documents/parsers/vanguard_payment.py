"""Vanguard ETF distribution advices from Computershare: the direct credit advice for one fund (2015 to 2022), and
the distribution payment statement listing each fund held (2022 on)."""

import re
from datetime import date as Date
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'vanguard_payment'
KIND = 'text'

ADVICE = 'Distribution Payment for the Period Ending'
STATEMENT = 'Vanguard Distribution for the Period Ended'
AMOUNT = r'\$?\d[\d,]*\.\d{2}'
# `ETF $0.26725861 1,005 $268.59`
ADVICE_ROW = re.compile(rf'^ETF[ \t]+\$?(\d+\.\d+)[ \t]+(\d[\d,]*)[ \t]+({AMOUNT})[ \t]*$', re.MULTILINE)
# `VGS* Vanguard MSCI Index International Shares ETF 0.30346465 4,231 1,283.96 2.00 1,281.96`: the asterisk marks
# a holding with no TFN. A long fund name wraps round the row, so the pattern keeps to the row's own line: the
# line before can be the end of a name, `ETF`.
STATEMENT_ROW = re.compile(rf'^({_common.CODE})\*?(?:[ \t]+\D[^\n]*?)?[ \t]+(\d+\.\d+)[ \t]+(\d[\d,]*)[ \t]+({AMOUNT})'
                           rf'[ \t]+({AMOUNT})[ \t]+({AMOUNT})[ \t]*$', re.MULTILINE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (_common.has_all(text, 'Computershare', 'Vanguard')
            and (_common.has_all(text, ADVICE, 'Direct Credit Advice', 'Rate Per Security')
                 or _common.has_all(text, STATEMENT, 'Distribution Payment', 'Tax Withheld')))


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    payment_date = _common.date(_common.labelled(text, 'Payment Date', _common.DATE))
    if _common.has_all(text, STATEMENT):
        rows = STATEMENT_ROW.findall(text)
        if not rows:
            raise _common.ParseError('No fund row of code, cash per security, units and amounts found under '
                                     '"Cash per Security".')
        # A row the pattern missed would leave the funds short of the statement's total.
        total = _common.labelled(text, 'Total:')
        if sum(_common.money(row[3]) for row in rows) != _common.money(total):
            raise _common.ParseError(f'The fund rows do not add up to the "Total:" of {total}.')
        return [_distribution(code, payment_date, rate, units, gross, withheld)
                for code, rate, units, gross, withheld, _net in rows]
    row = ADVICE_ROW.search(text)
    if row is None:
        raise _common.ParseError('No row of rate, securities held and gross amount found after "ETF".')
    code = _common.labelled(text, 'ASX Code', _common.CODE)
    return [_distribution(code, payment_date, row.group(1), row.group(2), row.group(3),
                          _common.labelled(text, 'Withholding tax'))]


def _distribution(code: str, payment_date: Date, rate: str, units: str, gross: str, withheld: str) -> Extracted:
    row: dict[str, Any] = {
        'instrument__name': code,
        'date': payment_date,
        'quantity': _common.units(units),
        'distribution_amount_per_share': _common.money(rate),
        'distribution_amount_per_share_currency': 'AUD',
    }
    if _common.money(withheld):
        row['total_withholding_tax'] = _common.money(withheld)
        row['total_withholding_tax_currency'] = 'AUD'
    return Extracted('Distribution', row, stated_total=_common.money(gross))
