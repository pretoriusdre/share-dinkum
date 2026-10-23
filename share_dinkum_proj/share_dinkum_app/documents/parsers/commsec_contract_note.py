"""CommSec confirmation contract notes for one buy or one sale of ASX securities, from 2009 to 2018."""

import re

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'commsec_contract_note'
KIND = 'text'

BOUGHT = 'WE HAVE BOUGHT THE FOLLOWING SECURITIES FOR YOU'
SOLD = 'WE HAVE SOLD THE FOLLOWING SECURITIES FOR YOU'


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (_common.has_all(text, 'Commonwealth Securities Limited', 'CONFIRMATION')
            and (_common.has_all(text, BOUGHT) or _common.has_all(text, SOLD)))


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    model = 'Buy' if _common.has_all(text, BOUGHT) else 'Sell'
    # The code is on its own line, under the company's name.
    code = re.search(rf'^COMPANY:? .*\n({_common.CODE})\s*$', text, re.MULTILINE)
    if code is None:
        raise _common.ParseError('No code found on the line after "COMPANY".')
    trade_date = re.search(rf'^DATE:\s*({_common.DATE})', text, re.MULTILINE)
    if trade_date is None:
        raise _common.ParseError('No value found after "DATE".')
    # Earlier notes call it the contract note, later ones the confirmation.
    reference = (_common.labelled_or_none(text, 'CONTRACT NOTE', r'\d+')
                 or _common.labelled(text, 'CONFIRMATION NO', r'\d+'))
    row = {
        'instrument__name': code.group(1),
        'date': _common.date(trade_date.group(1)),
        'quantity': _common.units(_common.labelled(text, 'TOTAL UNITS')),
        'unit_price': _common.money(_common.labelled(text, 'AVERAGE PRICE')),
        'unit_price_currency': 'AUD',
        'total_brokerage': _common.money(_common.labelled(text, 'BROKERAGE & COSTS INCL GST')),
        'total_brokerage_currency': 'AUD',
    }
    return [Extracted(model, row, reference=reference,
                      stated_total=_common.money(_common.labelled(text, 'CONSIDERATION (AUD)')))]
