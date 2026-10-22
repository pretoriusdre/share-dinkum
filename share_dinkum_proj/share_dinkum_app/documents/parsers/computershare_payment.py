"""Computershare dividend advices of Australian companies: the payment advice (2013 on) and the newer dividend
payment statement (AFI from 2021), one dividend a page. A statement that is also an advice of reinvestment (AFI to
2021) gives the dividend, then a Buy for the shares allotted."""

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
# A row of the statement's table: `15,069 X A$0.14 = A$2,109.66 A$2,109.66 A$904.14`, or with its dividend type
# first (`Special 12,580 X A$0.08 = ...`) where an ordinary and a special dividend are paid together.
STATEMENT_ROW = re.compile(rf'^(?:[A-Z][a-z]+[ \t]+)?(\d[\d,]*)[ \t]+X[ \t]+(A?\$[\d.,]+)[ \t]+=[ \t]+({AMOUNT})'
                           rf'((?:[ \t]+{AMOUNT})*)[ \t]*$', re.MULTILINE)
APPLIED = re.compile(rf'amount applied to issue to you (\d[\d,]*) shares at (A?\$[\d.,]+) per share'
                     rf'(?: \(including [\d.]+% discount\))?:\s*({AMOUNT})', re.IGNORECASE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return _common.has_all(text, ISSUER) and bool(_advices(text))


def parse(doc: Document) -> list[Extracted]:
    # An older file can hold several advices, a page each.
    events = []
    for page in _advices(doc.text or ''):
        if STATEMENT not in page.lower():
            events.append(_advice(page))
            continue
        events.append(_statement(page))
        if 'advice of reinvestment' in page.lower():
            events.append(_reinvestment(page, events[-1]))
    return events


def _advices(text: str) -> list[str]:
    """The pages that are a dividend advice. Only a statement is read where shares were allotted."""
    return [page for page in text.split('\f')
            if (_common.has_all(page, 'Net Payment', 'Franking', 'Direct Credit Reference No')
                and 'allotted' not in page.lower())
            or (_common.has_all(page, 'Number of shares', 'Dividend rate', 'Franking credit')
                and STATEMENT in page.lower())]


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
    header = re.search(r'^(?:Dividend type\s+)?Number of shares\s+Dividend rate\s+Dividend paid\s+(.*)$', text,
                       re.MULTILINE | re.IGNORECASE)
    if header is None:
        raise _common.ParseError('No value found after "Number of shares Dividend rate Dividend paid".')
    names = [STATEMENT_COLUMNS[name.lower()] for name in
             re.findall(r'franked amount|unfranked amount|franking credit|withholding tax', header.group(1),
                        re.IGNORECASE)]
    rows = list(STATEMENT_ROW.finditer(text))
    if not rows:
        raise _common.ParseError('No row of shares X rate = amount found under "Number of shares".')
    # An ordinary and a special dividend paid together on the same shares are one payment: their rates are added.
    quantity = _common.units(rows[0].group(1))
    if any(_common.units(row.group(1)) != quantity for row in rows):
        raise _common.ParseError('The rows under "Number of shares" are for different numbers of shares.')
    figures = {'gross': Decimal(0), **{name: Decimal(0) for name in names}}
    per_share = Decimal(0)
    for row in rows:
        amounts = [_common.money(amount) for amount in row.group(4).split()]
        if len(amounts) != len(names):
            raise _common.ParseError(f'The table header names {len(names)} amounts, but its row has {len(amounts)}.')
        per_share += _common.money(row.group(2))
        for name, amount in [('gross', _common.money(row.group(3))), *zip(names, amounts)]:
            figures[name] += amount
    if len(rows) > 1:
        total = re.search(rf'^Total((?:[ \t]+{AMOUNT})+)[ \t]*$', text, re.MULTILINE)
        if total is None or [_common.money(amount) for amount in total.group(1).split()] != \
                [figures['gross'], *(figures[name] for name in names)]:
            raise _common.ParseError('The rows under "Number of shares" do not add up to the "Total" line.')
    withheld = figures.pop('withheld', None)
    event = _dividend(text, _code(text), _common.labelled(text, 'Payment date', _common.DATE),
                      quantity, per_share, figures, None if withheld is None else str(withheld), None)
    lic = _common.labelled_or_none(text, 'The attributable part of the above dividend is', AMOUNT)
    if lic is not None:
        event.row['lic_capital_gain'] = _common.money(lic)
        event.row['lic_capital_gain_currency'] = 'AUD'
    return event


def _reinvestment(text: str, dividend: Extracted) -> Extracted:
    """The shares allotted for the dividend under the reinvestment plan, at the issue price, with no brokerage.

    The plan's cash account is checked: the balance brought forward plus the dividend, less the amount applied,
    must leave the balance carried forward.
    """
    applied = APPLIED.search(' '.join(text.split()))
    if applied is None:
        raise _common.ParseError('No value found after "the amount applied to issue to you".')
    quantity, price, cost = (_common.units(applied.group(1)), _common.money(applied.group(2)),
                             _common.money(applied.group(3)))
    allotted = _common.units(_common.labelled(text, 'Number of shares allotted for the reinvestment of this dividend:'))
    if allotted != quantity:
        raise _common.ParseError(f'{allotted} shares allotted, but the amount applied was for {quantity}.')
    amount = _common.money(_common.labelled(text, 'Dividend amount:', AMOUNT))
    brought = _common.money(_common.labelled(text, 'account brought forward:', AMOUNT))
    available = _common.money(_common.labelled(text, 'Amount available for reinvestment:', AMOUNT))
    carried = _common.money(_common.labelled(text, 'Your cash balance carried forward in your Plan account:', AMOUNT))
    if amount > (dividend.stated_total or 0) or brought + amount != available or available - cost != carried:
        raise _common.ParseError(f'The plan account does not add up: {brought} brought forward + {amount} dividend '
                                 f'= {available} available, less {cost} applied = {carried} carried forward.')
    row: dict[str, Any] = {
        'instrument__name': dividend.row['instrument__name'],
        'date': _common.date(_common.labelled(text, 'Allotment date', _common.DATE)),
        'quantity': quantity,
        'unit_price': price,
        'unit_price_currency': 'AUD',
        # The shares are issued for the whole amount applied: there is no brokerage.
        'total_brokerage': Decimal(0),
        'total_brokerage_currency': 'AUD',
    }
    return Extracted('Buy', row, stated_total=cost)


def _code(text: str) -> str:
    code = _common.labelled_or_none(text, 'ASX Code', _common.CODE)
    if code is None:
        # Some advices print the code only in the mailing line, `T 022783 000 QBE`, or on the line after it:
        # `000 Your security holding`, then `AFI`.
        mailing = (re.search(rf'^T \d+ 000 ({_common.CODE})\b', text, re.MULTILINE)
                   or re.search(rf'^000 Your security holding\n({_common.CODE})$', text, re.MULTILINE))
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
