"""Link Market Services payment advices, and the same registry's under its new name, MUFG Corporate Markets (2025
on). Three layouts:

- the payment advice table, a `DIVIDEND STATEMENT` or `DISTRIBUTION STATEMENT` (2012 on);
- BetaShares' distribution advice letter (2017 on);
- the `Key Details` statement, for a distribution (HPI 2021) or a dividend (AFI from 2025, with its LIC capital gain).

A capital repayment statement and a dividend reinvestment advice are not read here."""

import re
from decimal import Decimal
from typing import Any

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'link_payment'
KIND = 'text'

# Some advices (Coca-Cola Amatil, OFX) name the registry only in its web address.
REGISTRIES = ('Link Market Services', 'linkmarketservices.com.au', 'MUFG Corporate Markets')
AMOUNT = r'A?\$\d[\d,]*\.\d{2}'
# A rate per share or unit: `$0.0325`, `A$0.096`, `3.269623 cents`.
DOLLAR_RATE = r'A?\$\d+(?:\.\d+)?'
RATE = rf'{DOLLAR_RATE}|\d+(?:\.\d+)?[ \t]+cents'
# The money columns of a table, as its header names them after `Participating`: the advice's `Unfranked Franked
# Total Franking`, a distribution's `Distribution Unfranked Franked Total Franking`, or the Key Details dividend's
# `Franked Dividend Franking` (amount, paid, credit).
COLUMNS = {'Distribution': 'distribution', 'Unfranked': 'unfranked', 'Franked': 'franked', 'Total': 'gross',
           'Dividend': 'gross', 'Franking': 'credit', 'Withholding': 'withheld'}
WITHHELD = re.compile(rf'^\*?Less:?[^\n$]*Withholding Tax:?[ \t]*({AMOUNT})', re.MULTILINE | re.IGNORECASE)
# `This dividend is franked in Australia at 100% and the corporate tax rate used to determine the franking credit
# is 30%.`
FRANKED_PERCENT = re.compile(r'franked\s+in\s+Australia\s+at\s+(\d+(?:\.\d+)?)%', re.IGNORECASE)
TAX_RATE = re.compile(r'corporate\s+tax\s+rate\s+used\s+to\s+determine\s+the\s+franking\s+credit\s+is\s+(\d+(?:\.\d+)?)%',
                      re.IGNORECASE)


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return any(_common.has_all(text, registry) for registry in REGISTRIES) and _layout(text) is not None


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    layout = _layout(text)
    if layout == 'advice':
        return [_advice(text)]
    if layout == 'letter':
        return [_letter(text)]
    return [_key_details(text)]


def _layout(text: str) -> str | None:
    if (_common.has_all(text, 'Payment Advice', 'Security Description', 'Net Amount')
            and (_common.has_all(text, 'DIVIDEND STATEMENT') or _common.has_all(text, 'DISTRIBUTION STATEMENT'))):
        return 'advice'
    if _common.has_all(text, 'Distribution Advice', 'Class Description', 'Rate per Unit', 'Gross Amount'):
        return 'letter'
    if (_common.has_all(text, 'Key Details', 'PAYMENT REFERENCE NUMBER')
            and re.search(r'(?:dividend|distribution) statement for the', text, re.IGNORECASE)):
        return 'key details'
    return None


def _advice(text: str) -> Extracted:
    """The payment advice: `BWX - FULLY PAID ORDINARY $0.0325 3,056 $0.00 $99.32 $99.32 $42.57`."""
    header = re.search(r'^Security Description\b.*$', text, re.MULTILINE)
    if header is None:
        raise _common.ParseError('No table header starting "Security Description" found.')
    names = _columns(header.group(0))
    row = re.search(rf'^{_common.CODE} - [^\n]*?[ \t]+({RATE})[ \t]+(\d[\d,]*)((?:[ \t]+{AMOUNT})+)[ \t]*$',
                    text[header.end():], re.MULTILINE)
    if row is None:
        raise _common.ParseError('No row of code, rate, units and amounts found under "Security Description".')
    figures = _figures(names, row.group(3))
    withheld = WITHHELD.search(text)
    reference = _common.labelled(text, 'DIRECT CREDIT REFERENCE NO.', r'\d+')
    if _common.has_all(text, 'DISTRIBUTION STATEMENT'):
        return _distribution(text, _rate(row.group(1)), row.group(2), figures,
                             withheld.group(1) if withheld else None, reference)

    per_share = _rate(row.group(1))
    franked_rate = _common.labelled_or_none(text, 'Franked Rate per Share', DOLLAR_RATE)
    if franked_rate is not None:
        franked = _common.money(franked_rate)
    elif figures.get('franked') == 0:
        franked = Decimal(0)
    else:
        raise _common.ParseError('No value found after "Franked Rate per Share".')
    tax_rate = _common.labelled_or_none(text, 'Company Tax Rate', r'\d+(?:\.\d+)?')
    return _dividend(text, per_share, franked, row.group(2), figures, withheld.group(1) if withheld else None,
                     tax_rate, reference)


def _letter(text: str) -> Extracted:
    """BetaShares' letter: `Ordinary Units 3.269623 cents 3,940 $128.82` under `Class Description`."""
    header = re.search(r'^Class Description\b.*$', text, re.MULTILINE)
    if header is None:
        raise _common.ParseError('No table header starting "Class Description" found.')
    row = re.search(rf'^[^\n$\d]*?[ \t]+({RATE})[ \t]+(\d[\d,]*)[ \t]+({AMOUNT})[ \t]*$', text[header.end():],
                    re.MULTILINE)
    if row is None:
        raise _common.ParseError('No row of rate, units and gross amount found under "Class Description".')
    withheld = WITHHELD.search(text)
    return _distribution(text, _rate(row.group(1)), row.group(2), {'gross': _common.money(row.group(3))},
                         withheld.group(1) if withheld else None,
                         _common.labelled(text, 'Direct Credit Reference No.', r'\d+'))


def _key_details(text: str) -> Extracted:
    """The Key Details statement. A distribution: `Stapled Securities A$0.096 10,619 $1,019.42 $0.00 $1,019.42`
    (gross, withholding, net). A dividend: `A$0.17 15,069 A$2,561.73 A$2,561.73 A$1,097.88`, its description
    wrapping round the row."""
    reference = _common.labelled(text, 'PAYMENT REFERENCE NUMBER', r'\d+')
    if re.search(r'distribution statement for the', text, re.IGNORECASE):
        row = re.search(rf'^[^\n$\d]*?[ \t]+({RATE})[ \t]+(\d[\d,]*)[ \t]+({AMOUNT})[ \t]+({AMOUNT})[ \t]+({AMOUNT})'
                        rf'[ \t]*$', text, re.MULTILINE)
        if row is None:
            raise _common.ParseError('No row of rate, securities, gross, withholding and net amounts found under '
                                     '"Class".')
        return _distribution(text, _rate(row.group(1)), row.group(2), {'gross': _common.money(row.group(3))},
                             row.group(4), reference)

    header = re.search(r'^Dividend[ \t]+Participating\b.*$', text, re.MULTILINE)
    if header is None:
        raise _common.ParseError('No table header starting "Dividend Participating" found.')
    names = _columns(header.group(0))
    row = re.search(rf'^({RATE})[ \t]+(\d[\d,]*)((?:[ \t]+{AMOUNT})+)[ \t]*$', text[header.end():], re.MULTILINE)
    if row is None:
        raise _common.ParseError('No row of rate, shares and amounts found under "Dividend Participating".')
    figures = _figures(names, row.group(3))
    per_share = _rate(row.group(1))
    flat = ' '.join(text.split())
    percent = FRANKED_PERCENT.search(flat)
    if percent is not None:
        franked = per_share * Decimal(percent.group(1)) / 100
    elif figures.get('franked') == 0:
        franked = Decimal(0)
    else:
        raise _common.ParseError('No value found for the percentage franked ("franked in Australia at").')
    tax_rate = TAX_RATE.search(flat)
    withheld = figures.pop('withheld', None)
    event = _dividend(text, per_share, franked, row.group(2), figures, None if withheld is None else str(withheld),
                      tax_rate.group(1) if tax_rate else None, reference)
    lic = _common.labelled_or_none(text, 'The attributable part of the above dividend is', AMOUNT)
    if lic is not None:
        event.row['lic_capital_gain'] = _common.money(lic)
        event.row['lic_capital_gain_currency'] = 'AUD'
    return event


def _columns(header: str) -> list[str]:
    """The money columns a table header names, after `Participating`."""
    after = header.split('Participating', 1)[-1]
    return [COLUMNS[word] for word in re.findall(r'\b(' + '|'.join(COLUMNS) + r')\b', after)]


def _figures(names: list[str], amounts: str) -> dict[str, Decimal]:
    values = [_common.money(amount) for amount in amounts.split()]
    if len(values) != len(names):
        raise _common.ParseError(f'The table header names {len(names)} amounts, but its row has {len(values)}.')
    figures = dict(zip(names, values))
    if 'gross' not in figures:
        raise _common.ParseError('No "Total Payment" or "Dividend Paid" column in the table header.')
    return figures


def _rate(text: str) -> Decimal:
    if 'cents' in text:
        return _common.cents_to_dollars(_common.money(text.replace('cents', '')))
    return _common.money(text)


def _dividend(text: str, per_share: Decimal, franked: Decimal, units: str, figures: dict[str, Decimal],
              withheld: str | None, tax_rate: str | None, reference: str) -> Extracted:
    quantity = _common.units(units)
    if 'franked' in figures and abs(franked * quantity - figures['franked']) > Decimal('0.01'):
        raise _common.ParseError(f'The franked part, {franked} a share, does not give the franked amount '
                                 f'{figures["franked"]} on {quantity} shares.')
    row: dict[str, Any] = {
        'instrument__name': _common.labelled(text, 'ASX Code', _common.CODE),
        'date': _common.date(_common.labelled(text, 'Payment Date', _common.DATE)),
        'quantity': quantity,
        'dividend_type': 'LOCAL',
        'unfranked_amount_per_share': per_share - franked,
        'unfranked_amount_per_share_currency': 'AUD',
        'franked_amount_per_share': franked,
        'franked_amount_per_share_currency': 'AUD',
    }
    if withheld is not None and _common.money(withheld):
        row['local_withholding_tax'] = _common.money(withheld)
        row['local_withholding_tax_currency'] = 'AUD'
    if tax_rate is not None and franked:
        row['corporate_tax_rate_percentage'] = Decimal(tax_rate)
    return Extracted('Dividend', row, reference=reference, stated_total=figures['gross'])


def _distribution(text: str, per_unit: Decimal, units: str, figures: dict[str, Decimal], withheld: str | None,
                  reference: str) -> Extracted:
    if figures.get('franked') or figures.get('credit'):
        raise _common.ParseError('This distribution is franked, which a distribution record cannot hold.')
    row: dict[str, Any] = {
        'instrument__name': _common.labelled(text, 'ASX Code', _common.CODE),
        'date': _common.date(_common.labelled(text, 'Payment Date', _common.DATE)),
        'quantity': _common.units(units),
        'distribution_amount_per_share': per_unit,
        'distribution_amount_per_share_currency': 'AUD',
    }
    if withheld is not None and _common.money(withheld):
        row['total_withholding_tax'] = _common.money(withheld)
        row['total_withholding_tax_currency'] = 'AUD'
    return Extracted('Distribution', row, reference=reference, stated_total=figures['gross'])
