"""Computershare dividend advices of Australian companies: the payment advice (2013 on) and the newer dividend
payment statement (AFI from 2021), one dividend a page. A dividend reinvestment advice is not read here."""

import re
from decimal import Decimal
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'computershare_payment'
KIND = 'text'

ISSUER = 'Computershare'
STATEMENT = 'dividend payment statement'

# A rate per share: `2.0 Cents`, `6 cents`, `1.320040 cents`, `$1.27`, `A$0.63526192`.
RATE = r'A?\$\s?\d[\d,]*(?:\.\d+)?|\d+(?:\.\d+)?\s*cents?'
AMOUNT = r'A?\$?\s?\d[\d,]*\.\d{2}'
ROW = re.compile(rf'(?P<rate>{RATE})\s+(?P<units>\d[\d,]*)\s+(?P<amounts>{AMOUNT}(?:\s+{AMOUNT})*)\s*$',
                 re.IGNORECASE)
# The money columns of the advice's table, as its header names them: `Franked Unfranked Gross`, or QBE's
# `Franked Unfranked Franking Dividend` (the franking credit, then the payment).
HEADER_COLUMNS = {'Franked': 'franked', 'Unfranked': 'unfranked', 'Franking': 'credit', 'Gross': 'gross',
                  'Dividend': 'gross', 'Total': 'gross'}
STATEMENT_COLUMNS = {'franked amount': 'franked', 'unfranked amount': 'unfranked', 'franking credit': 'credit',
                     'withholding tax': 'withheld'}
# `This payment is 30% franked`, `fully franked (100%)`, `franked in Australia at 100%`.
FRANKED_PERCENT = re.compile(r'(\d+(?:\.\d+)?)%\s*franked|franked\s+in\s+Australia\s+at\s+(\d+(?:\.\d+)?)%',
                             re.IGNORECASE)
TAX_RATE = re.compile(r'tax\s+rate\s+(?:used\s+to\s+determine\s+the\s+franking\s+credit\s+is|of)\s+(\d+(?:\.\d+)?)%',
                      re.IGNORECASE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return _common.has_all(text, ISSUER) and bool(_advices(text))


def parse(doc: Document) -> list[Extracted]:
    # An older file can hold several advices, a page each.
    return [_statement(page) if STATEMENT in page.lower() else _advice(page) for page in _advices(doc.text or '')]


def _advices(text: str) -> list[str]:
    """The pages that are a dividend advice. A reinvestment advice says how many shares were allotted."""
    return [page for page in text.split('\f')
            if (_common.has_all(page, 'Net Payment', 'Franking', 'Direct Credit Reference No')
                or (_common.has_all(page, 'Number of shares', 'Dividend rate', 'Franking credit')
                    and STATEMENT in page.lower()))
            and 'allotted' not in page.lower()]


def _advice(text: str) -> Extracted:
    """The payment advice: a table row of rate, shares and amounts under a header starting `Class`."""
    header = re.search(r'^Class\b.*$', text, re.MULTILINE)
    if header is None:
        raise _common.ParseError('No table header starting "Class" found.')
    money_header = re.search(r'\b(?:Franked|Gross)\b.*', header.group(0))
    if money_header is None:
        raise _common.ParseError('No "Franked" or "Gross" column in the table header.')
    names = [HEADER_COLUMNS[word] for word in
             re.findall(r'\b(Unfranked|Franked|Franking|Gross|Dividend|Total)\b', money_header.group(0))]

    # The row follows the header, before `Net Payment`. A long class description can wrap round it.
    table = text[header.end():].split('Net Payment', 1)[0]
    row = next((match for line in table.splitlines() if (match := ROW.search(line))), None)
    if row is None:
        raise _common.ParseError('No row of rate, shares and amounts found under the "Class" header.')
    amounts = [_common.money(amount) for amount in row.group('amounts').split()]
    if len(amounts) != len(names):
        raise _common.ParseError(f'The table header names {len(names)} amounts, but its row has {len(amounts)}.')
    figures = dict(zip(names, amounts))
    if 'gross' not in figures:
        raise _common.ParseError('No "Gross" column in the table header.')

    rate = row.group('rate')
    per_share = _common.cents_to_dollars(_common.money(rate.lower().replace('cents', '').replace('cent', ''))) \
        if 'cent' in rate.lower() else _common.money(rate)
    quantity = _common.units(row.group('units'))
    withheld = re.search(rf'Less\b[^\n$]*?withholding\s+tax\s+({_common.NUMBER})', text, re.IGNORECASE)
    reference = _common.labelled(text, 'Direct Credit Reference No.', r'\d+')
    return _dividend(text, _code(text), _common.labelled(text, 'Payment Date', _common.DATE), quantity, per_share,
                     figures, withheld.group(1) if withheld else None, reference)


def _statement(text: str) -> Extracted:
    """The newer statement: `15,069 X A$0.14 = A$2,109.66 A$2,109.66 A$904.14` under its header."""
    header = re.search(r'^Number of shares\s+Dividend rate\s+Dividend paid\s+(.*)$', text,
                       re.MULTILINE | re.IGNORECASE)
    if header is None:
        raise _common.ParseError('No value found after "Number of shares Dividend rate Dividend paid".')
    names = [STATEMENT_COLUMNS[name.lower()] for name in
             re.findall(r'franked amount|unfranked amount|franking credit|withholding tax', header.group(1),
                        re.IGNORECASE)]
    row = re.search(rf'^(\d[\d,]*)[ \t]+X[ \t]+(A?\$[\d.,]+)[ \t]+=[ \t]+({AMOUNT})((?:[ \t]+{AMOUNT})*)[ \t]*$', text,
                    re.MULTILINE)
    if row is None:
        raise _common.ParseError('No row of shares X rate = amount found under "Number of shares".')
    amounts = [_common.money(amount) for amount in row.group(4).split()]
    if len(amounts) != len(names):
        raise _common.ParseError(f'The table header names {len(names)} amounts, but its row has {len(amounts)}.')
    figures = {'gross': _common.money(row.group(3)), **dict(zip(names, amounts))}
    withheld = figures.pop('withheld', None)
    event = _dividend(text, _code(text), _common.labelled(text, 'Payment date', _common.DATE),
                      _common.units(row.group(1)), _common.money(row.group(2)), figures,
                      None if withheld is None else str(withheld), None)
    lic = _common.labelled_or_none(text, 'The attributable part of the above dividend is', AMOUNT)
    if lic is not None:
        event.row['lic_capital_gain'] = _common.money(lic)
        event.row['lic_capital_gain_currency'] = 'AUD'
    return event


def _code(text: str) -> str:
    code = _common.labelled_or_none(text, 'ASX Code', _common.CODE)
    if code is None:
        # Some advices print the code only in the mailing line: `T 022783 000 QBE`.
        mailing = re.search(rf'^T \d+ 000 ({_common.CODE})\b', text, re.MULTILINE)
        if mailing is None:
            raise _common.ParseError('No value found after "ASX Code".')
        code = mailing.group(1)
    return code


def _dividend(text: str, code: str, payment_date: str, quantity: Decimal, per_share: Decimal,
              figures: dict[str, Decimal], withheld: str | None, reference: str | None) -> Extracted:
    franked, unfranked = _franked_split(text, per_share, figures)
    if 'franked' in figures and abs(franked * quantity - figures['franked']) > Decimal('0.01'):
        raise _common.ParseError(f'The franked part, {franked} a share, does not give the franked amount '
                                 f'{figures["franked"]} on {quantity} shares.')
    row: dict[str, Any] = {
        'instrument__name': code,
        'date': _common.date(payment_date),
        'quantity': quantity,
        'dividend_type': 'LOCAL',
        'unfranked_amount_per_share': unfranked,
        'unfranked_amount_per_share_currency': 'AUD',
        'franked_amount_per_share': franked,
        'franked_amount_per_share_currency': 'AUD',
    }
    if withheld is not None and _common.money(withheld):
        row['local_withholding_tax'] = _common.money(withheld)
        row['local_withholding_tax_currency'] = 'AUD'
    tax_rate = TAX_RATE.search(' '.join(text.split()))
    if tax_rate is not None and franked:
        row['corporate_tax_rate_percentage'] = Decimal(tax_rate.group(1))
    return Extracted('Dividend', row, reference=reference, stated_total=figures['gross'])


def _franked_split(text: str, per_share: Decimal, figures: dict[str, Decimal]) -> tuple[Decimal, Decimal]:
    """The franked and unfranked parts of the rate per share.

    The advice prints only the rate, so a wholly franked or unfranked dividend is all one part. A partly
    franked one is split by the percentage franked the advice states (`This payment is 30% franked`).
    """
    if figures.get('unfranked') == 0 and figures.get('franked'):
        return per_share, Decimal(0)
    if figures.get('franked') == 0 and figures.get('unfranked'):
        return Decimal(0), per_share
    flat = ' '.join(text.split())
    if re.search(r'\bfully\s+franked\b', flat, re.IGNORECASE):
        return per_share, Decimal(0)
    if re.search(r'\bdividend\s+is\s+unfranked\b', flat, re.IGNORECASE):
        return Decimal(0), per_share
    percent = FRANKED_PERCENT.search(flat)
    if percent is None:
        raise _common.ParseError('No value found for the percentage franked ("% franked").')
    share = Decimal(percent.group(1) or percent.group(2))
    if share == 100:
        return per_share, Decimal(0)
    franked = per_share * share / 100
    return franked, per_share - franked
