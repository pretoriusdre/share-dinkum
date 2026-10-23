"""Interactive Brokers activity statement, as CSV (2021 on): its share trades, dividends and distributions with the
tax withheld, and each holding at the end of the period.

The statement is many tables one under another, each row starting with its section's name and `Header`, `Data`,
`SubTotal` or `Total`. Times are US Eastern (the statement says `EDT`), so a trade is dated in its exchange's time
zone: an ASX trade at `2022-10-02, 22:11:25` was made on 3 October in Sydney. Currency conversions, a payment in
lieu of a dividend (paid on shares out on loan, not a dividend), withholding on interest and stock awards are not
read here."""

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'ibkr_export'
KIND = 'table'

HEADER = ['Statement', 'Header', 'Field Name', 'Field Value']
STATEMENT_ZONES = {'EDT': 'America/New_York', 'EST': 'America/New_York'}
# `A200 (AU00000A2000) Cash Dividend AUD 3.523673 (Mixed Income)`, `IBKR(US45841N1072) Cash Dividend USD 0.10 per
# Share (Ordinary Dividend)`; its tax, `... Cash Dividend AUD 3.523673 - AU Tax`.
DIVIDEND = re.compile(r'^(?P<key>(?P<symbol>[A-Z0-9.]+) ?\((?P<isin>[A-Z]{2}[A-Z0-9]{10})\) Cash Dividend '
                      r'(?P<currency>[A-Z]{3}) (?P<rate>\d+(?:\.\d+)?)(?: per Share)?)(?: \(.*\))?$')
TAX = re.compile(r'^(?P<key>.*Cash Dividend .*?) - [A-Z]{2} Tax$')
PERIOD = re.compile(r'^\w+ \d{1,2}, \d{4} - (\w+ \d{1,2}, \d{4})$')
UNIT_PLACES = Decimal('0.0001')


def recognises(doc: Document) -> bool:
    table = doc.table
    if table is None or [str(column).strip() for column in table.columns][:4] != HEADER:
        return False
    return any(row[:2] == ['Data', 'BrokerName'] and 'Interactive Brokers' in row[2] for row in _rows(doc, 'Statement'))


def parse(doc: Document) -> list[Extracted]:
    statement = {row[1]: row[2] for row in _rows(doc, 'Statement') if row[0] == 'Data'}
    zone = STATEMENT_ZONES.get(statement.get('WhenGenerated', '').rsplit(' ', 1)[-1])
    if zone is None:
        raise _common.ParseError('No US Eastern time zone (EDT or EST) after "WhenGenerated".')
    # An instrument can be listed under more than one symbol (`VWRA, VWRA.L`), the first its own.
    instruments = {alias.strip(): {**record, 'Symbol': record['Symbol'].split(',')[0].strip()}
                   for record in _records(doc, 'Financial Instrument Information')
                   for alias in record['Symbol'].split(',')}
    events = [_trade(record, instruments, zone) for record in _records(doc, 'Trades')
              if record.get('DataDiscriminator') == 'Order' and record.get('Asset Category') == 'Stocks']
    events += _payments(doc, instruments)
    events += _holdings(doc, statement)
    return events


def _rows(doc: Document, section: str) -> list[list[str]]:
    """The rows of `section`, less the section's name, in file order."""
    assert doc.table is not None
    rows = [[str(column) for column in doc.table.columns], *doc.table.astype(str).values.tolist()]
    return [row[1:] for row in rows if row and row[0] == section]


def _records(doc: Document, section: str) -> list[dict[str, str]]:
    """The `Data` rows of `section` as {column: value}, by the `Header` row above each."""
    records, header = [], None
    for row in _rows(doc, section):
        if row[0] == 'Header':
            header = row[1:]
        elif row[0] == 'Data' and header is not None:
            records.append({name: value.strip() for name, value in zip(header, row[1:]) if name})
    return records


def _trade(record: dict[str, str], instruments: dict[str, dict[str, str]], zone: str) -> Extracted:
    instrument = instruments.get(record['Symbol'], {})
    symbol, exchange = instrument.get('Symbol', record['Symbol']), instrument.get('Listing Exch', '')
    if not exchange:
        raise _common.ParseError(f'No "Listing Exch" for {symbol} under "Financial Instrument Information".')
    when = datetime.strptime(record['Date/Time'], '%Y-%m-%d, %H:%M:%S')
    quantity = _common.units(record['Quantity'])
    currency = record['Currency']
    row: dict[str, Any] = {
        'instrument__name': symbol,
        'date': _common.exchange_date(when, zone, exchange),
        'quantity': abs(quantity),
        'unit_price': _common.money(record['T. Price']),
        'unit_price_currency': currency,
        'total_brokerage': abs(_common.money(record['Comm/Fee'])),
        'total_brokerage_currency': currency,
    }
    return Extracted('Buy' if quantity > 0 else 'Sell', row, stated_total=abs(_common.money(record['Proceeds'])))


def _payments(doc: Document, instruments: dict[str, dict[str, str]]) -> list[Extracted]:
    """Each cash dividend or distribution, with the tax withheld from it.

    The tax is matched by its description, and every row of it added: a tax reversed and charged again later
    nets to what was withheld.
    """
    withheld: dict[str, Decimal] = {}
    for record in _records(doc, 'Withholding Tax'):
        tax = TAX.match(record.get('Description', ''))
        if tax:
            withheld[tax['key']] = withheld.get(tax['key'], Decimal(0)) - _common.money(record['Amount'])
    events = []
    for record in _records(doc, 'Dividends'):
        dividend = DIVIDEND.match(record.get('Description', ''))
        if dividend is None or record['Currency'].startswith('Total'):
            continue
        events.append(_payment(record, dividend, instruments, withheld.pop(dividend['key'], Decimal(0))))
    left = {key: amount for key, amount in withheld.items() if amount}
    if left:
        raise _common.ParseError(f'Tax withheld from a dividend not in this statement: {", ".join(left)}.')
    return events


def _payment(record: dict[str, str], dividend: re.Match[str], instruments: dict[str, dict[str, str]],
             withheld: Decimal) -> Extracted:
    symbol, currency = dividend['symbol'], dividend['currency']
    rate, amount = _common.money(dividend['rate']), _common.money(record['Amount'])
    row: dict[str, Any] = {'instrument__name': symbol, 'date': _common.date(record['Date']),
                           'quantity': _units(amount, rate)}
    if instruments.get(symbol, {}).get('Type') == 'ETF':
        row |= {'distribution_amount_per_share': rate, 'distribution_amount_per_share_currency': currency}
        if withheld:
            row |= {'total_withholding_tax': withheld, 'total_withholding_tax_currency': currency}
        return Extracted('Distribution', row, stated_total=amount)
    row |= {'dividend_type': 'LOCAL' if dividend['isin'].startswith('AU') else 'FOREIGN',
            'unfranked_amount_per_share': rate, 'unfranked_amount_per_share_currency': currency,
            'franked_amount_per_share': Decimal(0), 'franked_amount_per_share_currency': currency}
    if withheld:
        column = 'local_withholding_tax' if dividend['isin'].startswith('AU') else 'foreign_tax_credit'
        row |= {column: withheld, f'{column}_currency': currency}
    return Extracted('Dividend', row, stated_total=amount)


def _units(amount: Decimal, rate: Decimal) -> Decimal:
    """The units paid on, which the statement does not print: the whole number that gives the amount within a cent
    (the broker rounds a payment up as often as to the nearest), or else (a fractional holding) the amount over the
    rate, to the places units are stored to."""
    whole = (amount / rate).quantize(Decimal(1))
    if abs(whole * rate - amount) <= Decimal('0.01'):
        return whole
    return (amount / rate).quantize(UNIT_PLACES)


def _holdings(doc: Document, statement: dict[str, str]) -> list[Extracted]:
    """Each share holding at the end of the period, from `Open Positions`."""
    period = PERIOD.match(statement.get('Period', ''))
    if period is None:
        raise _common.ParseError('No "Month D, YYYY - Month D, YYYY" after "Period".')
    day = datetime.strptime(period.group(1), '%B %d, %Y').date()
    return [_holding(record['Symbol'], day, record['Quantity']) for record in _records(doc, 'Open Positions')
            if record.get('DataDiscriminator') == 'Summary' and record.get('Asset Category') == 'Stocks']


def _holding(symbol: str, day: date, quantity: str) -> Extracted:
    return Extracted('HoldingBalance', {'instrument__name': symbol, 'date': day, 'quantity': _common.units(quantity)})
