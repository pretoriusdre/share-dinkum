"""Boardroom dividend statements (Ramsay Health Care, Metcash, Sirtex; 2012 on). An older file can hold several
advices, a page each. A scheme payment statement is not read here. No payment reference is printed.

The text layer of the newer statements breaks numbers with spaces (`27 0 81.5 c ents $220 .05 N il`), and scans
read `Nil` as `I!N`, so the table row is read with its spaces taken out."""

import re
from decimal import Decimal
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'boardroom_payment'
KIND = 'text'

# The row's cells after the rate: gross, withholding, net, franked, unfranked, franking credit. Nil is printed for
# a zero amount.
CELLS = ('gross', 'withheld', 'net', 'franked', 'unfranked', 'credit')
# A date with stray spaces: `07/03/202 5`, `28-09-2017`.
SPACED_DATE = r'\d[\d/ -]*\d'


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return _common.has_all(text, 'Boardroom') and bool(_advices(text))


def parse(doc: Document) -> list[Extracted]:
    return [_dividend(page) for page in _advices(doc.text or '')]


def _advices(text: str) -> list[str]:
    return [page for page in text.split('\f')
            if _common.has_all(page, 'Dividend Statement', 'Payment Description', 'Franking', 'Payment Date')]


def _dividend(text: str) -> Extracted:
    # The row is the one line with the rate in cents, `c ents` as the text layer can print it.
    line = next((line for line in text.splitlines() if re.search(r'\d\s*c\s*e\s*n\s*t\s*s\s*\$', line)), None)
    if line is None:
        raise _common.ParseError('No row of shares held, rate in cents and amounts found under "Number of shares".')
    flat = ''.join(line.split())
    leading, cells = re.split(r'cents', flat, maxsplit=1)
    tokens = re.findall(r'\$[\d,]+\.\d{2}|[^$]+', cells)
    if len(tokens) != len(CELLS):
        raise _common.ParseError(f'The row has {len(tokens)} cells after the rate, not {len(CELLS)}.')
    figures = {name: _common.money(token) if token.startswith('$') else Decimal(0)
               for name, token in zip(CELLS, tokens)}
    quantity, per_share = _units_and_rate(leading, figures['gross'])

    if not figures['unfranked']:
        franked = per_share
    elif not figures['franked']:
        franked = Decimal(0)
    else:
        percent = re.search(r'dividend\s+is\s+(\d+(?:\.\d+)?)%\s*franked', text, re.IGNORECASE)
        if percent is None:
            raise _common.ParseError('No value found for the percentage franked ("This dividend is N% franked").')
        franked = per_share * Decimal(percent.group(1)) / 100
    if abs(franked * quantity - figures['franked']) > Decimal('0.01'):
        raise _common.ParseError(f'The franked part, {franked} a share, does not give the franked amount '
                                 f'{figures["franked"]} on {quantity} shares.')
    row: dict[str, Any] = {
        'instrument__name': _common.labelled(text, 'ASX Code', _common.CODE),
        'date': _common.date(_common.labelled(text, 'Payment Date', SPACED_DATE).replace(' ', '')),
        'quantity': quantity,
        'dividend_type': 'LOCAL',
        'unfranked_amount_per_share': per_share - franked,
        'unfranked_amount_per_share_currency': 'AUD',
        'franked_amount_per_share': franked,
        'franked_amount_per_share_currency': 'AUD',
    }
    if figures['withheld']:
        row['local_withholding_tax'] = figures['withheld']
        row['local_withholding_tax_currency'] = 'AUD'
    tax_rate = re.search(r'calculated\s+at\s+the\s+rate\s+of\s+(\d+(?:\.\d+)?)%', text, re.IGNORECASE)
    if tax_rate is not None and franked:
        row['corporate_tax_rate_percentage'] = Decimal(tax_rate.group(1))
    return Extracted('Dividend', row, stated_total=figures['gross'])


def _units_and_rate(leading: str, gross: Decimal) -> tuple[Decimal, Decimal]:
    """The shares held and the rate a share, from their digits run together (`27081.5`).

    With the spaces gone the split between them is lost, so take the one split where shares times rate gives the
    gross amount.
    """
    digits = leading.replace(',', '')
    if not re.fullmatch(r'\d+(?:\.\d+)?', digits):
        raise _common.ParseError(f'{leading!r} is not a number of shares followed by a rate in cents.')
    whole = digits.split('.')[0]
    found = []
    for cut in range(1, len(whole)):
        units, cents = digits[:cut], digits[cut:]
        if cents.startswith('0') and not cents.startswith('0.'):
            continue
        rate = _common.cents_to_dollars(Decimal(cents))
        if rate and abs(Decimal(units) * rate - gross) <= Decimal('0.01'):
            found.append((Decimal(units), rate))
    if len(found) != 1:
        raise _common.ParseError(f'Cannot tell the shares held from the rate in {leading!r}.')
    return found[0]
