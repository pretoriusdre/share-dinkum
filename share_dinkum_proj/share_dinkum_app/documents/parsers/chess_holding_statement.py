"""ASX's CHESS holding statement (2009 on): the holding brought forward, then each movement with the balance after
it. Each balance is a HoldingBalance, checked against the units the portfolio's trades give on that day.

Older statements are scans, read by OCR; the newest text layer has lost its spaces (`31Aug26`,
`BalanceBroughtForwardfrom21Jun21`), so every pattern allows a space to be missing."""

import re
from datetime import date, datetime
from decimal import Decimal

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'chess_holding_statement'
KIND = 'text'

DAY = r'\d{1,2}\s?[A-Z][a-z]{2}\s?\d{2}'
UNITS = r'\d[\d,]*'
# The security above the table: `AFI- ORDINARY FULLY PAID`, `VAS -VANGUARD AUSTRALIAN SHARES INDEX ETF`.
SECURITY = re.compile(rf'^({_common.CODE})\s?-\s?[A-Z]', re.MULTILINE)
BROUGHT_FORWARD = re.compile(rf'^Balance\s?Brought\s?Forward\s?from\s?({DAY})\s+({UNITS})\s*$', re.MULTILINE)
# `31 Aug 18  Allotment of Securities due to  6089500071525800  279  12580`: the date, the transaction's
# type and id (`001731777AV9MO00`), the units on or off, and the balance after it. OCR can lose the units.
MOVEMENT = re.compile(rf'^({DAY})\s+(\S.*?)\s+(\w{{12,}})\s+(?:(?i:Ex|Cum)\s+)?(?:({UNITS})\s+)?({UNITS})\s*$',
                      re.MULTILINE)
FINAL = re.compile(rf'^Final\s?Holding\s?Balance\s?to\s?({DAY})\s+({UNITS})\s*$', re.MULTILINE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return _common.has_all(text, 'CHESS HOLDING STATEMENT', 'Transaction ID')


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    code = _code(text)
    balances: dict[date, Decimal] = {}
    held: Decimal | None = None
    brought = BROUGHT_FORWARD.search(text)
    if brought is not None:
        held = _common.units(brought.group(2))
        balances[_day(brought.group(1))] = held
    movements = list(MOVEMENT.finditer(text))
    if not movements and brought is None:
        raise _common.ParseError('No "Balance Brought Forward" and no movement found under "Transaction Type".')
    for movement in movements:
        balance = _common.units(movement.group(5))
        if movement.group(4) is None:
            # OCR lost the units moved: the balance is all there is to check.
            held = balance
            balances[_day(movement.group(1))] = balance
            continue
        moved = _common.units(movement.group(4))
        # On or off: the text does not say which column the units are in, so the balance must be either.
        if held is not None and balance not in (held + moved, held - moved):
            raise _common.ParseError(f'On {movement.group(1)}, {moved} units moved take {held} to {held + moved} or '
                                     f'{held - moved}, not the balance {balance}: is a figure misread?')
        if held is None and balance != moved:
            raise _common.ParseError(f'The first movement, on {movement.group(1)}, has no balance brought forward, '
                                     f'so its {moved} units should be the balance, not {balance}.')
        held = balance
        # The last balance of the day is the holding at its end.
        balances[_day(movement.group(1))] = balance
    final = FINAL.search(text)
    if final is not None:
        if held is not None and _common.units(final.group(2)) != held:
            raise _common.ParseError(f'The final holding balance, {final.group(2)}, is not the last balance, {held}.')
        balances[_day(final.group(1))] = _common.units(final.group(2))
    return [Extracted('HoldingBalance', {'instrument__name': code, 'date': day, 'quantity': units})
            for day, units in sorted(balances.items())]


def _code(text: str) -> str:
    header = re.search(r'Transaction\s?Type', text)
    if header is None:
        raise _common.ParseError('No table header with "Transaction Type" found.')
    found = list(SECURITY.finditer(text, 0, header.start()))
    if not found:
        raise _common.ParseError('No security ("CODE - DESCRIPTION") found above the table.')
    return found[-1].group(1)


def _day(text: str) -> date:
    return datetime.strptime(''.join(text.split()), '%d%b%y').date()
