"""Computershare annual tax statements for Vanguard's ETFs: the AMMA statement (2018 on) and the annual taxation
statement before it (2015 to 2017).

Part B gives the components, each from its last column (attribution, or taxable income before AMIT), except the CGT
concession amount, which is cash. Part A gives the credits: franking (13Q), foreign resident withholding (13A) and
the foreign income tax offset (20O). Three forms of the AMMA statement are read:

- 2018 to 2020: the labels are printed first and the figures after them in a block of their own, in the same
  order, Part A's then Part B's;
- 2021 to 2023: each line carries its figures;
- 2024 on: as 2021, with the cash column left blank but for the totals.
"""

import re
from decimal import Decimal

from share_dinkum_app.documents.extracted import Document, Extracted
from share_dinkum_app.documents.parsers import _common

NAME = 'computershare_annual_statement'
KIND = 'text'

AMIT_HEADING = 'ATTRIBUTION MANAGED INVESTMENT TRUST MEMBER ANNUAL TAX STATEMENT'
PRE_AMIT_HEADING = 'ANNUAL TAXATION STATEMENT FOR THE YEAR ENDED'
AMOUNT = r'\(?-?\$\d[\d,]*\.\d{2}\)?'
AMOUNTS_ONLY = re.compile(rf'^(?:{AMOUNT}[ \t]*)+$')
# `Net capital gain 18A $210.32`, `Foreign income tax offset 20O* $38.15`, or the label alone when its figure is
# printed further down.
PART_A_ROW = re.compile(rf'^(.*?)\s(\d{{2}}[A-Z])\*?(?:[ \t]+({AMOUNT}))?[ \t]*$')
# `T 050410 000 VAE`: the holder's mailing reference, then the fund's code.
CODE_LINE = re.compile(rf'^(?:T[ \t]+)?\d{{6}}[ \t]+000[ \t]+({_common.CODE})\b', re.MULTILINE)
YEAR_END = re.compile(r'YEAR\s+ENDED\s+(\d{1,2}\s+[A-Za-z]+\s+\d{4})', re.IGNORECASE)

# Part A's labels the credits come from.
CREDITS = {'13Q': 'FRANKING_CREDIT', '13A': 'WITHHOLDING_CREDIT', '20O': 'FOREIGN_INCOME_TAX_OFFSET'}

# Part B's lines, by label as `_label` writes it, and the component each gives. Several lines give one component
# where the statement splits it (NCMI, clean building MIT income), and their figures are added. `None` is a line
# read only to check the others against, or one no component holds.
SUBTOTAL = 'non primary production income'
GROSS = 'gross amount'
NIL_ONLY = {'net exempt income'}
LINES: dict[str, str | None] = {
    'unfranked distributions': 'UNFRANKED_DISTRIBUTION',
    'unfranked cfi distributions': 'UNFRANKED_DISTRIBUTION',
    'interest': 'INTEREST',
    'interest - subject to non resident wht': 'INTEREST',
    'interest - not subject to non resident wht': 'INTEREST',
    'interest - subject to non-resident withholding tax': 'INTEREST',
    'interest - not subject to non-resident withholding tax': 'INTEREST',
    'other income': 'OTHER_INCOME',
    'other income - clean building mit income': 'OTHER_INCOME',
    'other income - excluded from ncmi': 'OTHER_INCOME',
    'other income - ncmi': 'OTHER_INCOME',
    SUBTOTAL: None,
    'franked distributions': 'FRANKED_DISTRIBUTION',
    'discounted capital gains tap': 'DISCOUNTED_TAP',
    'discounted capital gains tap - clean building mit income': 'DISCOUNTED_TAP',
    'discounted capital gains tap - excluded from ncmi': 'DISCOUNTED_TAP',
    'discounted capital gains tap - ncmi': 'DISCOUNTED_TAP',
    'discounted capital gains ntap': 'DISCOUNTED_NTAP',
    'cgt concession amount': 'CGT_CONCESSION',
    'capital gains - other method tap': 'OTHER_TAP',
    'capital gains - other method tap - clean building mit income': 'OTHER_TAP',
    'capital gains - other method tap - excluded from ncmi': 'OTHER_TAP',
    'capital gains - other method tap - ncmi': 'OTHER_TAP',
    'capital gains - other method ntap': 'OTHER_NTAP',
    'distributed capital gains': None,
    'net capital gain': 'NET_CAPITAL_GAIN',
    'amit cgt gross up amount': 'AMIT_GROSS_UP',
    'other capital gains distribution': 'OTHER_CG_DISTRIBUTION',
    'total current year capital gains': 'TOTAL_CY_CG',
    'assessable foreign source income': 'FOREIGN_SOURCE_INCOME',
    'non-assessable non-exempt amount': 'NON_ASSESSABLE_NON_EXEMPT',
    'net exempt income': None,
    'other non-attributable amounts': 'NON_ATTRIBUTABLE',
    'tax-free amounts': 'TAX_FREE',
    'tax-deferred amounts': 'TAX_DEFERRED',
    'tax-exempt amounts': 'TAX_EXEMPT',
    GROSS: None,
    'gross cash distribution': None,
    'amit cost base net amount - excess (reduce cost base)': 'COSTBASE_DECREASE',
    'amit cost base net amount - shortfall (increase cost base)': 'COSTBASE_INCREASE',
    'less tfn amounts withheld': None,
    'tfn amounts withheld': None,
    '- interest / dividend amounts withheld': None,
    '- fund payment amounts withheld': None,
    'amounts withheld for foreign resident withholding': None,
    'less other expenses': None,
    'other expenses': None,
    'net cash distribution': None,
    # Before AMIT, a section after Part B breaks down label 18H.
    'capital gains - discount method (grossed up amount)': None,
    'capital gains - other method': None,
}
# The CGT concession amount is cash only: its taxable income column is always nil.
FIRST_COLUMN = {'CGT_CONCESSION'}
INCOME = ('UNFRANKED_DISTRIBUTION', 'INTEREST', 'OTHER_INCOME')


def recognises(doc: Document) -> bool:
    text = doc.text or ''
    return (_common.has_all(text, 'Computershare', 'COMPONENTS OF')
            and (_common.has_all(text, AMIT_HEADING) or _common.has_all(text, PRE_AMIT_HEADING)))


def parse(doc: Document) -> list[Extracted]:
    page = _statement_page(doc.text or '')
    code = CODE_LINE.search(page)
    if code is None:
        raise _common.ParseError('No fund code found after the mailing reference ("T 000000 000 VAE").')
    year_end = YEAR_END.search(page)
    if year_end is None:
        raise _common.ParseError('No date found after "YEAR ENDED".')
    keys = {'instrument__name': code.group(1), 'financial_year_end_date': _common.date(year_end.group(1))}

    part_a, part_b = _rows(page)
    found: dict[str, Decimal] = {}
    for label, figures in part_a:
        if label in CREDITS:
            found[CREDITS[label]] = _common.money(figures[-1])
    by_line: dict[str, Decimal] = {}
    for label, figures in part_b:
        if label in by_line:
            raise _common.ParseError(f'Part B has the line "{label}" twice.')
        component = LINES[label]
        figure = _common.money(figures[0] if component in FIRST_COLUMN else figures[-1])
        by_line[label] = figure
        if label in NIL_ONLY and figure:
            raise _common.ParseError(f'Part B gives {figures[-1]} on "{label}", which no component holds.')
        if component is not None:
            found[component] = found.get(component, Decimal(0)) + figure
    _check_totals(by_line, found)

    events = [Extracted('AttributionStatement', dict(keys))]
    events += [Extracted('AttributionComponent', {'component': component, 'amount': amount,
                                                  'amount_currency': 'AUD'})
               for component, amount in found.items() if amount]
    adjustment = _cost_base_change(found, by_line)
    if adjustment is not None:
        events.append(Extracted('CostBaseAdjustment', {**keys, 'cost_base_increase': adjustment,
                                                       'cost_base_increase_currency': 'AUD'}))
    return events


def _statement_page(text: str) -> str:
    """The page with Part B on it, the first; a later page is the guide to reading it."""
    for page in text.split('\n\f\n'):
        if _common.has_all(page, 'COMPONENTS OF'):
            return page
    raise _common.ParseError('No page has "PART B – COMPONENTS OF".')


def _label(text: str) -> str:
    """A line's label, lower case with its dashes and spaces made plain and any note number dropped."""
    text = re.sub(r'[–—�]', '-', text)
    text = re.sub(r'\s\d{1,2}$', '', ' '.join(text.split()))
    return text.lower()


def _rows(page: str) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """Part A's rows as (label code, figures) and Part B's as (label, figures), in the order printed."""
    lines = [line.strip() for line in page.splitlines()]
    try:
        start_a = next(n for n, line in enumerate(lines) if re.match(r'PART A\b', line))
        start_b = next(n for n, line in enumerate(lines) if re.match(r'PART B\b', line))
        end_b = next(n for n, line in enumerate(lines) if line.startswith('PLEASE RETAIN'))
    except StopIteration:
        raise _common.ParseError('The page lacks one of "PART A", "PART B" and "PLEASE RETAIN".') from None

    part_a: list[tuple[str, list[str]]] = []
    for line in lines[start_a + 1:start_b]:
        match = PART_A_ROW.match(line)
        if match:
            part_a.append((match.group(2), [match.group(3)] if match.group(3) else []))
    part_b: list[tuple[str, list[str]]] = []
    for line in lines[start_b + 1:end_b]:
        figures = re.findall(AMOUNT, line)
        label = _label(re.split(AMOUNT, line, maxsplit=1)[0])
        if label in LINES:
            part_b.append((label, figures))
        elif figures:
            raise _common.ParseError(f'Part B has a line this parser does not know: "{label}".')
        # A line with no figures is a heading or a note (the 2024 tax hub link). In the 2018 form, where no line
        # has figures, an unknown line with a figure further down leaves the figures one over, which is caught.

    if any(figures for _, figures in part_b):
        missing = [label for label, figures in part_b if not figures]
        if missing:
            raise _common.ParseError(f'Part B gives no figure on "{missing[0]}".')
        return part_a, part_b
    return _aligned(lines, part_a, part_b)


def _aligned(lines: list[str], part_a: list[tuple[str, list[str]]],
             part_b: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
    """The 2018 to 2020 form: the figures follow the labels as lines of figures alone, Part A's first."""
    figure_lines = [re.findall(AMOUNT, line) for line in lines if AMOUNTS_ONLY.match(line)]
    if len(figure_lines) != len(part_a) + len(part_b):
        raise _common.ParseError(f'Parts A and B have {len(part_a) + len(part_b)} labels but {len(figure_lines)} '
                                 'lines of figures.')
    return ([(label, figures) for (label, _), figures in zip(part_a, figure_lines)],
            [(label, figures) for (label, _), figures in zip(part_b, figure_lines[len(part_a):])])


def _check_totals(by_line: dict[str, Decimal], found: dict[str, Decimal]) -> None:
    """The statement's own subtotals agree with its lines, so a line read from the wrong column shows."""
    def part(component: str) -> Decimal:
        return found.get(component, Decimal(0))

    if SUBTOTAL in by_line:
        income = sum((part(component) for component in INCOME), Decimal(0))
        if income != by_line[SUBTOTAL]:
            raise _common.ParseError(f'The income lines come to {income}, but "{SUBTOTAL}" is {by_line[SUBTOTAL]}.')
    if GROSS in by_line and SUBTOTAL in by_line:
        attributed = (by_line[SUBTOTAL] + part('FRANKED_DISTRIBUTION') + part('TOTAL_CY_CG')
                      + part('FOREIGN_SOURCE_INCOME') + part('NON_ASSESSABLE_NON_EXEMPT'))
        if attributed != by_line[GROSS]:
            raise _common.ParseError(f'The attribution column comes to {attributed}, but "{GROSS}" is '
                                     f'{by_line[GROSS]}.')


def _cost_base_change(found: dict[str, Decimal], by_line: dict[str, Decimal]) -> Decimal | None:
    """The AMIT cost base net amount, an increase less a decrease; before AMIT, less the tax-deferred amount."""
    if any(label.startswith('amit cost base net amount') for label in by_line):
        return found.get('COSTBASE_INCREASE', Decimal(0)) - found.get('COSTBASE_DECREASE', Decimal(0))
    if 'tax-deferred amounts' in by_line:
        return Decimal(0) - found['TAX_DEFERRED']
    return None
