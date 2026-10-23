"""Link Market Services annual tax statements, and the same registry's under its new name, MUFG Corporate Markets:
the AMMA statement (BetaShares from 2018, HPI from 2024), and the annual tax statement of a trust outside AMIT (HPI
to 2023). A file holding several years' statements is read statement by statement.

The components table ("Components of attribution", or "of distribution") gives each component from its last
column, the attributable or taxable amount, except the CGT concession amount, which is cash. The tax return summary
gives the credits (13Q, 13A, 20O), and the AMIT cost base lines follow the table.
"""

import re
from decimal import Decimal

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'link_annual_statement'
KIND = 'text'

REGISTRIES = ('Link Market Services', 'linkmarketservices.com.au', 'MUFG Corporate Markets')
AMOUNT = r'\(?-?\$\d[\d,]*\.\d{2}\)?'
FIGURES_ONLY = re.compile(rf'^(?:{AMOUNT}[ \t]*)+$')
# `Attribution Managed Investment Trust Member Annual Statement` then `for the year ended 30 June 2022`, or
# `Annual Tax Statements for the year ended 30 June 2016`.
HEADING = re.compile(r'(?:Attribution\s+Managed\s+Investment\s+Trust\s+Member\s+Annual\s+Statement|Annual\s+Tax\s+'
                     r'Statements?)\s+for\s+the\s+year\s+ended\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', re.IGNORECASE)
TABLE_START = re.compile(r'Components\s+of\s+(?:attribution|distribution)', re.IGNORECASE)
# The 2020 statements print `Net Cash Distributition`.
TABLE_END = re.compile(r'^Net\s+Cash\s+Distribut', re.IGNORECASE | re.MULTILINE)
# `Foreign income tax offset * $133.29 20O`.
CREDIT = re.compile(rf'({AMOUNT})[ \t]+(13Q|13A|20O)[ \t]*$', re.MULTILINE)
CREDITS = {'13Q': 'FRANKING_CREDIT', '13A': 'WITHHOLDING_CREDIT', '20O': 'FOREIGN_INCOME_TAX_OFFSET'}
# `AMIT cost base net amount - excess (decrease cost base) $0.00`, or in 2021 `AMIT cost base net increase amount`.
# The 2019 statements follow the excess and shortfall with a net increase and net reduction, which are not read.
COST_BASE = re.compile(rf'^AMIT\s+cost\s+base\s+net\s+(?:amount\s+-\s+(excess|shortfall)\s+\([^)]*\)|(increase|decrease)'
                       rf'\s+amount)[ \t]*({AMOUNT})', re.IGNORECASE | re.MULTILINE)
COST_BASE_SIDE = {'excess': 'COSTBASE_DECREASE', 'decrease': 'COSTBASE_DECREASE',
                  'shortfall': 'COSTBASE_INCREASE', 'increase': 'COSTBASE_INCREASE'}

SUBTOTAL = 'subtotal'
# A line no component holds, which must be nil.
NIL = 'nil'
# The other net foreign source income, read only where the table has no assessable foreign source income line.
OTHER_FOREIGN = 'other foreign'
# Each table line, by label as `_label` writes it, and the component it gives. Several lines give one component
# where the statement splits it (NCMI, CBMI, interest subject or not to withholding), and their figures are added.
# `None` is a total, a cash line or a breakdown of lines already counted.
LINES: list[tuple[str, str | None]] = [
    (r'interest \((?:not )?subject to non-resident withholding tax\)', 'INTEREST'),
    (r'dividends - unfranked (?:cfi )?amount(?: (?:not )?declared to be cfi)?', 'UNFRANKED_DISTRIBUTION'),
    (r'other assessable australian income(?: \((?:other|ncmi|excluded from ncmi|cbmi)\))?', 'OTHER_INCOME'),
    (r'other income', 'OTHER_INCOME'),
    (r'(?:excluded from )?ncmi - non-primary production', 'OTHER_INCOME'),
    (r'clean building mit income', 'OTHER_INCOME'),
    (r'dividends - less lic capital gain deduction', NIL),
    (r'less other allowable trust deductions', NIL),
    (r'non-primary production income', SUBTOTAL),
    (r'dividends - franked amount(?: \(franked distributions\))?', 'FRANKED_DISTRIBUTION'),
    (r'capital gains - discounted method tap\b.*', 'DISCOUNTED_TAP'),
    (r'capital gains - discounted method ntap\b.*', 'DISCOUNTED_NTAP'),
    (r'capital gains - other method tap\b.*', 'OTHER_TAP'),
    (r'capital gains - other method ntap\b.*', 'OTHER_NTAP'),
    (r'capital gains - indexation method n?tap', NIL),
    (r'cgt concession amount n?tap', 'CGT_CONCESSION'),
    (r'taxable foreign capital gains - discounted method', 'FOREIGN_CG_DISCOUNTED'),
    (r'taxable foreign capital gains - other method', 'FOREIGN_CG_OTHER'),
    (r'distributed capital gains', None),
    (r'net capital gains', 'NET_CAPITAL_GAIN'),
    (r'amit cgt gross up amount', 'AMIT_GROSS_UP'),
    (r'other capital gains distribution', 'OTHER_CG_DISTRIBUTION'),
    (r'total current year capital gains', 'TOTAL_CY_CG'),
    (r'(?:excluded from )?ncmi capital gains', None),
    (r'assessable foreign (?:source )?income', 'FOREIGN_SOURCE_INCOME'),
    (r'other net foreign source income', OTHER_FOREIGN),
    (r'australian franking credit from a new zealand franking company', NIL),
    (r'cfc income', NIL),
    (r'transferor trust income', NIL),
    (r'total foreign income', None),
    (r'net exempt income', NIL),
    (r'non-assessable non-exempt amount', 'NON_ASSESSABLE_NON_EXEMPT'),
    (r'other non-attributable amounts', 'NON_ATTRIBUTABLE'),
    (r'(?:other )?tax-free amounts', 'TAX_FREE'),
    (r'tax-deferred amounts', 'TAX_DEFERRED'),
    (r'tax-exempted amounts', 'TAX_EXEMPT'),
    (r'return of capital', NIL),
    # A letter lost to the font leaves `Total(cid:81)on-assessable amounts`.
    (r'total n?on-assessable amounts', None),
    (r'cash distribution sub-total', None),
    (r'gross cash distribution', None),
    (r'less:? .*', None),
]
# The CGT concession amount is cash only: its taxable column is nil.
FIRST_COLUMN = {'CGT_CONCESSION'}
INCOME = ('INTEREST', 'UNFRANKED_DISTRIBUTION', 'OTHER_INCOME')

Rows = list[tuple[str, list[str]]]


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (any(_common.has_all(text, registry) for registry in REGISTRIES)
            and HEADING.search(text) is not None and TABLE_START.search(text) is not None)


def parse(doc: Document) -> list[Extracted]:
    text = doc.text or ''
    # A statement may repeat its heading on a later page.
    headings: list[re.Match[str]] = []
    for heading in HEADING.finditer(text):
        if not headings or _common.date(heading.group(1)) != _common.date(headings[-1].group(1)):
            headings.append(heading)
    events: list[Extracted] = []
    for number, heading in enumerate(headings):
        end = headings[number + 1].start() if number + 1 < len(headings) else len(text)
        events += _statement(text, heading, end)
    return events


def _statement(text: str, heading: re.Match[str], end: int) -> list[Extracted]:
    """The events of the statement whose heading is `heading`, reading up to `end`."""
    body = text[heading.start():end]
    codes = re.findall(rf'ASX\s+Code:?\s*({_common.CODE})\b', text[:end])
    if not codes:
        raise _common.ParseError('No value found after "ASX Code".')
    keys = {'instrument__name': codes[-1], 'financial_year_end_date': _common.date(heading.group(1))}

    found: dict[str, Decimal] = {}
    for amount, label in CREDIT.findall(body):
        found[CREDITS[label]] = _common.money(amount)
    by_line: dict[str, list[Decimal]] = {}
    for label, figures in _rows(body):
        component = _component(label)
        figure = _common.money(figures[0] if component in FIRST_COLUMN else figures[-1])
        by_line.setdefault(component or '', []).append(figure)
        if component == NIL and figure:
            raise _common.ParseError(f'The statement gives {figures[-1]} on "{label}", which no component holds.')
        if component and component not in (NIL, SUBTOTAL, OTHER_FOREIGN):
            found[component] = found.get(component, Decimal(0)) + figure
    if 'FOREIGN_SOURCE_INCOME' not in found and OTHER_FOREIGN in by_line:
        found['FOREIGN_SOURCE_INCOME'] = sum(by_line[OTHER_FOREIGN], Decimal(0))
    _check_subtotal(found, by_line)

    cost_base = {COST_BASE_SIDE[(old or new).lower()]: _common.money(amount)
                 for old, new, amount in COST_BASE.findall(body)}
    if cost_base:
        found['COSTBASE_DECREASE'] = cost_base.get('COSTBASE_DECREASE', Decimal(0))
        found['COSTBASE_INCREASE'] = cost_base.get('COSTBASE_INCREASE', Decimal(0))

    events = [Extracted('AttributionStatement', dict(keys))]
    events += [Extracted('AttributionComponent', {'component': component, 'amount': amount,
                                                  'amount_currency': 'AUD'})
               for component, amount in found.items() if amount]
    if cost_base:
        adjustment: Decimal | None = found['COSTBASE_INCREASE'] - found['COSTBASE_DECREASE']
    elif 'TAX_DEFERRED' in found:
        adjustment = Decimal(0) - found['TAX_DEFERRED']
    else:
        adjustment = None
    if adjustment is not None:
        events.append(Extracted('CostBaseAdjustment', {**keys, 'cost_base_increase': adjustment,
                                                       'cost_base_increase_currency': 'AUD'}))
    return events


def _label(text: str) -> str:
    """A line's label, lower case, plain dashes and spaces, `TARP` as `TAP`, and no `(A)` or `(A+X+B+C)` after."""
    text = re.sub(r'\(cid:\d+\)', ' ', text)
    text = re.sub(r'[–—�]', '-', text)
    text = re.sub(r'\s*\((?:[A-Z]\+)*[A-Z]\)\s*$', '', text.strip())
    text = ' '.join(text.split()).lower()
    text = text.replace('non - resident', 'non-resident').replace('tax deferred', 'tax-deferred')
    text = re.sub(r'\b(n?)tarp\b', r'\1tap', text)
    return re.sub(r'\b(method|amount) - (n?tap)\b', r'\1 \2', text)


def _component(label: str) -> str | None:
    for pattern, component in LINES:
        if re.fullmatch(pattern, label):
            return component
    raise _common.ParseError(f'The components table has a line this parser does not know: "{label}".')


def _known(label: str) -> bool:
    return any(re.fullmatch(pattern, label) for pattern, _ in LINES)


def _rows(body: str) -> Rows:
    """The components table's lines as (label, figures), in the order printed.

    A figure printed on a line of its own belongs to the label just before it that has none, or failing that the
    one just after.
    """
    start = TABLE_START.search(body)
    end = TABLE_END.search(body)
    if start is None:
        raise _common.ParseError('No table headed "Components of attribution" or "of distribution" found.')
    if end is None:
        raise _common.ParseError('No "Net Cash Distribution" after the components table: is its last page missing?')
    lines = [line.strip() for line in body[start.end():end.start()].splitlines()]
    rows: Rows = []
    loose: dict[int, list[str]] = {n: re.findall(AMOUNT, line) for n, line in enumerate(lines)
                                   if FIGURES_ONLY.match(line)}
    for n, line in enumerate(lines):
        if FIGURES_ONLY.match(line):
            continue
        figures = re.findall(AMOUNT, line)
        label = _label(re.split(AMOUNT, line, maxsplit=1)[0])
        if not _known(label):
            if figures:
                raise _common.ParseError(f'The components table has a line this parser does not know: "{label}".')
            continue
        if not figures:
            if n + 1 in loose:
                figures = loose.pop(n + 1)
            elif n - 1 in loose:
                figures = loose.pop(n - 1)
            else:
                continue
        rows.append((label, figures))
    left = [figure for figures in loose.values() for figure in figures if _common.money(figure)]
    if left:
        raise _common.ParseError(f'The components table has a figure, {left[0]}, on no line of its own.')
    return rows


def _check_subtotal(found: dict[str, Decimal], by_line: dict[str, list[Decimal]]) -> None:
    """The income lines agree with the non-primary production income, so a line read wrongly shows."""
    if SUBTOTAL not in by_line:
        return
    income = sum((found.get(component, Decimal(0)) for component in INCOME), Decimal(0))
    if income != by_line[SUBTOTAL][0]:
        raise _common.ParseError(f'The income lines come to {income}, but the non-primary production income is '
                                 f'{by_line[SUBTOTAL][0]}.')
