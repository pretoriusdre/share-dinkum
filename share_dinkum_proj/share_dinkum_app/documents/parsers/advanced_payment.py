"""Advanced Share Registry dividend advices, in two layouts: the dividend
distribution statement (to 2017) and the letter with its `Amount per Security` table. An older file can hold
several advices, a page each. The letter's `Ref. No.` is not a payment reference (later ones all print 1), so
no reference is taken."""

import re
from decimal import Decimal
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'advanced_payment'
KIND = 'text'

# A letter can print an amount with a space after the dollar sign: `$ 588.80`.
AMOUNT = r'\$[ \t]?\d[\d,]*\.\d{2}'
# `SOL ORDINARY $0.18000 680 $0.00 $122.40 $52.45`: unfranked, franked, franking credit.
STATEMENT_ROW = re.compile(rf'^({_common.CODE})\b[^\n$]*?[ \t]+\$(\d+\.\d+)[ \t]+(\d[\d,]*)[ \t]+({AMOUNT})[ \t]+'
                           rf'({AMOUNT})[ \t]+({AMOUNT})[ \t]*$', re.MULTILINE)
# `Final Dividend 32 cents 1,840 $ 588.80 $ 588.80`: franked amount, then gross payment. Scanned copies lose the
# header's order, so the columns are taken by position.
LETTER_ROW = re.compile(rf'^[A-Za-z ]*?Dividend[ \t]+(\d+(?:\.\d+)?)[ \t]*cents[ \t]+(\d[\d,]*)((?:[ \t]+{AMOUNT})+)'
                        rf'[ \t]*$', re.MULTILINE)
# `The dividend is 100% franked`, which the text layer prints as `1 00 franked` or `is100%franked`.
FRANKED_PERCENT = re.compile(r'dividend\s+is\s*(\d[\d ]*?)\s*%?\s*franked', re.IGNORECASE)
WITHHELD = re.compile(rf'(?:Withholding Tax|Tax With?held)[^\n$]*?({AMOUNT})', re.IGNORECASE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (any(_common.has_all(text, name) for name in ('Advanced Share Registry', 'advancedshare.com.au'))
            and bool(_advices(text)))


def parse(doc: Document) -> list[Extracted]:
    return [_statement(page) if _is_statement(page) else _letter(page) for page in _advices(doc.text or '')]


def _advices(text: str) -> list[str]:
    return [page for page in text.split('\f')
            if _is_statement(page) or _common.has_all(page, 'ASX Code', 'Payment Date', 'Amount per', 'Net Payment')]


def _is_statement(page: str) -> bool:
    return _common.has_all(page, 'Dividend Distribution Statement', 'Payable Date')


def _statement(text: str) -> Extracted:
    row = STATEMENT_ROW.search(text)
    if row is None:
        raise _common.ParseError('No row of class, dividend per share, shares and amounts found under "Security".')
    code, rate, units, unfranked, franked, _credit = row.groups()
    per_share, quantity = _common.money(rate), _common.units(units)
    if not _common.money(unfranked):
        franked_part = per_share
    elif not _common.money(franked):
        franked_part = Decimal(0)
    else:
        raise _common.ParseError('This dividend is partly franked, and the statement prints no franked rate per share.')
    tax_rate = re.search(r'Franked\s+at\s+(\d+(?:\.\d+)?)%', text, re.IGNORECASE)
    return _dividend(text, code, _common.labelled(text, 'Payable Date', _common.DATE), quantity, per_share,
                     franked_part, _common.money(franked),
                     _common.money(_common.labelled(text, 'Gross Payment Amount', AMOUNT)),
                     tax_rate.group(1) if tax_rate else None)


def _letter(text: str) -> Extracted:
    row = LETTER_ROW.search(text)
    if row is None:
        raise _common.ParseError('No row of dividend, cents per share, securities and amounts found under '
                                 '"Amount per".')
    amounts = re.findall(AMOUNT, row.group(3))
    if len(amounts) not in (2, 3):
        raise _common.ParseError(f'Expected a franked amount and a gross payment, but the row has {len(amounts)} '
                                 f'amounts.')
    per_share = _common.cents_to_dollars(_common.money(row.group(1)))
    percent = FRANKED_PERCENT.search(text)
    if percent is None:
        raise _common.ParseError('No value found for the percentage franked ("dividend is N% franked").')
    tax_rate = re.search(r'company\s+tax\s+rate\s+of\s+(\d+(?:\.\d+)?)', text, re.IGNORECASE)
    return _dividend(text, _common.labelled(text, 'ASX Code', _common.CODE),
                     _common.labelled(text, 'Payment Date', _common.DATE), _common.units(row.group(2)), per_share,
                     per_share * Decimal(percent.group(1).replace(' ', '')) / 100, _common.money(amounts[0]),
                     _common.money(amounts[-1]), tax_rate.group(1) if tax_rate else None)


def _dividend(text: str, code: str, payment_date: str, quantity: Decimal, per_share: Decimal, franked: Decimal,
              franked_amount: Decimal, gross: Decimal, tax_rate: str | None) -> Extracted:
    if abs(franked * quantity - franked_amount) > Decimal('0.01'):
        raise _common.ParseError(f'The franked part, {franked} a share, does not give the franked amount '
                                 f'{franked_amount} on {quantity} shares.')
    row: dict[str, Any] = {
        'instrument__name': code,
        'date': _common.date(payment_date),
        'quantity': quantity,
        'dividend_type': 'LOCAL',
        'unfranked_amount_per_share': per_share - franked,
        'unfranked_amount_per_share_currency': 'AUD',
        'franked_amount_per_share': franked,
        'franked_amount_per_share_currency': 'AUD',
    }
    withheld = WITHHELD.search(text)
    if withheld is not None and _common.money(withheld.group(1)):
        row['local_withholding_tax'] = _common.money(withheld.group(1))
        row['local_withholding_tax_currency'] = 'AUD'
    if tax_rate is not None and franked:
        row['corporate_tax_rate_percentage'] = Decimal(tax_rate)
    return Extracted('Dividend', row, stated_total=gross)
