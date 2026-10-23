"""SelfWealth buy and sell confirmations for one trade of ASX securities, from 2018 on.

The notes before 2020 charge brokerage on a separate tax invoice that follows the confirmation in
the same file, and show $0.00 brokerage on the confirmation itself.
"""

import re
from decimal import Decimal

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'selfwealth_contract_note'
KIND = 'text'

BOUGHT = 'WE HAVE BOUGHT ON YOUR ACCOUNT'
SOLD = 'WE HAVE SOLD ON YOUR ACCOUNT'

# One line of the trade table: quantity, code, the description (which may be on a line of its own),
# price, consideration and currency.
TRADE = re.compile(rf'^({_common.NUMBER}) ({_common.CODE})\b.*? (\d[\d,]*\.\d+) (\$[\d,]+\.\d{{2}}) ([A-Z]{{3}})\s*$',
                   re.MULTILINE)
# Every fee the confirmation lists. GST has its own line only on the earlier notes.
FEE = re.compile(r'^(?:Brokerage|Admin Fee|Adviser Fee|Misc Fees & Charges|GST)\*?:? (\$[\d,]+\.\d{2}) [A-Z]{3}',
                 re.MULTILINE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (_common.has_all(text, 'Quantity Security Code Security Description')
            and (_common.has_all(text, 'selfwealth.com.au') or _common.has_all(text, 'Branch Name: SELFWEALTH LIMITED'))
            and (_common.has_all(text, BOUGHT) or _common.has_all(text, SOLD)))


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    confirmation, _, invoice = text.partition('TAX INVOICE')
    model = 'Buy' if _common.has_all(confirmation, BOUGHT) else 'Sell'
    trades = TRADE.findall(confirmation)
    if len(trades) != 1:
        raise _common.ParseError(f'Expected one line under "Quantity Security Code", found {len(trades)}.')
    quantity, code, price, consideration, currency = trades[0]

    fees = sum((_common.money(fee) for fee in FEE.findall(confirmation)), Decimal(0))
    net = _common.money(_common.labelled_or_none(confirmation, 'Net Value')
                        or _common.labelled(confirmation, 'Total Amount Payable'))
    if net != _common.money(consideration) + (fees if model == 'Buy' else -fees):
        raise _common.ParseError('The fees listed do not account for the difference between "Consideration" '
                                 'and the net amount, so a fee was missed.')
    if invoice:
        fees += _common.money(_common.labelled(invoice, 'Paid'))
    elif _common.has_all(confirmation, 'Branch Name: SELFWEALTH LIMITED') and not fees:
        raise _common.ParseError('The brokerage is on a separate "TAX INVOICE", which is not in this file. '
                                 'Save the confirmation and its invoice as one file.')

    reference = _common.labelled(confirmation, 'Reference No', r'\S+').replace('­', '-')
    row = {
        'instrument__name': code,
        'date': _common.date(_common.labelled(confirmation, 'Trade Date', _common.DATE)),
        'quantity': _common.units(quantity),
        'unit_price': _common.money(price),
        'unit_price_currency': currency,
        'total_brokerage': fees,
        'total_brokerage_currency': currency,
    }
    return [Extracted(model, row, reference=reference, stated_total=_common.money(consideration))]
