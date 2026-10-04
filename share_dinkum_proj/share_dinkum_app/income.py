"""Assessable income for an Australian individual's tax return, by return label and year.

Dividends count in the year they are paid. A trust's income comes from its annual statement,
not from the cash, since the statement says what the cash was made of. Capital gains, from
trusts too, are left to the CGT report. Reads the database, never writes to it.

Residency decides what counts, by the status on the payment date (a statement's: on the last
day of its year):

* Resident: everything.
* Temporary resident: Australian income only. Foreign income is not assessable (s768-910).
* Foreign resident: nothing. Australian income paid to a foreign resident is taxed by
  withholding, if at all, not on a return, and foreign income is not assessable. It is listed
  separately, with what was withheld.
* No residency declared at all: resident is assumed and the year is flagged, as the CGT report
  does. A day the declared history does not cover is not counted, and is flagged.

Amounts are in the account's currency, at the rate stored on the payment, since income is
assessed when it is paid.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from djmoney.money import Money

from share_dinkum_app.cgt import discount, residency
from share_dinkum_app.choices import AttributionComponent, DividendType, TaxpayerType

if TYPE_CHECKING:
    from share_dinkum_app.models import Account, ExchangeRate, FiscalYear

ZERO = Decimal('0')

DIVIDEND = 'Dividend'
DISTRIBUTION = 'Distribution'

#: The return labels this report fills, in form order, with the form's wording.
LABELS: dict[str, str] = {
    '11S': 'Unfranked amount',
    '11T': 'Franked amount',
    '11U': 'Franking credit',
    '11V': 'TFN amounts withheld from dividends',
    '13U': 'Share of net income from trusts, less capital gains, foreign income and franked distributions',
    '13C': 'Franked distributions from trusts',
    '13Q': 'Share of franking credits from franked dividends',
    '13R': 'Share of credit for TFN amounts withheld',
    '13A': 'Share of credit for amounts withheld from foreign resident withholding',
    '20E': 'Assessable foreign source income',
    '20M': 'Other net foreign source income',
    '20O': 'Foreign income tax offsets',
}

#: Where each income line of a trust's statement goes on the return. Lines not here and not in
#: REFERENCE_COMPONENTS are capital gains or cost base movements, which are not income.
TRUST_LABELS: dict[str, str] = {
    AttributionComponent.UNFRANKED_DISTRIBUTION: '13U',
    AttributionComponent.INTEREST: '13U',
    AttributionComponent.OTHER_INCOME: '13U',
    AttributionComponent.FRANKED_DISTRIBUTION: '13C',
    AttributionComponent.FRANKING_CREDIT: '13Q',
    AttributionComponent.WITHHOLDING_CREDIT: '13A',
    AttributionComponent.FOREIGN_SOURCE_INCOME: '20M',
    AttributionComponent.FOREIGN_INCOME_TAX_OFFSET: '20O',
}
#: Statement lines shown for reference, on no label.
REFERENCE_COMPONENTS = (
    AttributionComponent.NON_ASSESSABLE_NON_EXEMPT,
    AttributionComponent.WITHHOLDING_DEDUCTED,
)
#: Statement lines that are foreign income, which a temporary resident does not count.
FOREIGN_COMPONENTS = (
    AttributionComponent.FOREIGN_SOURCE_INCOME,
    AttributionComponent.FOREIGN_INCOME_TAX_OFFSET,
)

#: Figures for the reference lines, beside the labels.
LIC_CAPITAL_GAIN = 'lic_capital_gain'
NON_ASSESSABLE = 'non_assessable'
EXCLUDED_CASH = 'excluded_cash'
EXCLUDED_WITHHELD = 'excluded_withheld'
FIGURE_KEYS = [*LABELS, LIC_CAPITAL_GAIN, NON_ASSESSABLE, EXCLUDED_CASH, EXCLUDED_WITHHELD]

#: A distribution paid this soon after a year starts is usually the last one of the year before,
#: and is explained by that year's statement.
LATE_PAYMENT_WINDOW = timedelta(days=92)


@dataclass(frozen=True)
class PaymentRow:
    """A dividend or distribution, in the account's currency. None where it has no rate."""

    kind: str
    record_id: Any
    legacy_id: str | None
    payment_date: date
    fiscal_year: str | None
    instrument: str
    dividend_type: str | None
    residency: str | None
    counts: bool
    treatment: str
    cash: Decimal | None
    unfranked: Decimal | None = None
    franked: Decimal | None = None
    franking_credit: Decimal | None = None
    withheld: Decimal | None = None
    foreign_tax_credit: Decimal | None = None
    lic_capital_gain: Decimal | None = None


@dataclass(frozen=True)
class TrustLine:
    """An income line of a trust's annual statement, and the return label it goes on."""

    statement_legacy_id: str | None
    fiscal_year: str | None
    year_end: date
    instrument: str
    component: str
    description: str
    label: str | None
    amount: Decimal | None
    residency: str | None
    counts: bool
    treatment: str


@dataclass
class IncomeSummary:
    """Every payment and statement line, with warnings by fiscal year name."""

    payments: list[PaymentRow] = field(default_factory=list)
    trust_lines: list[TrustLine] = field(default_factory=list)
    warnings: dict[str, list[str]] = field(default_factory=dict)

    def years(self) -> list[str]:
        """Fiscal years with a payment or a statement, earliest first."""
        names = {row.fiscal_year for row in self.payments} | {line.fiscal_year for line in self.trust_lines}
        return sorted(name for name in names if name)

    def year_warnings(self, year: str) -> list[str]:
        return list(self.warnings.get(year, []))

    def is_draft(self, year: str) -> bool:
        return bool(self.warnings.get(year))

    def figures(self, year: str) -> dict[str, Decimal]:
        """The year's total for each of FIGURE_KEYS."""
        totals = {key: ZERO for key in FIGURE_KEYS}

        def add(key: str, amount: Decimal | None) -> None:
            if amount is not None:
                totals[key] += amount

        for row in self.payments:
            if row.fiscal_year != year:
                continue
            if not row.counts:
                add(EXCLUDED_CASH, row.cash)
                add(EXCLUDED_WITHHELD, row.withheld)
            elif row.kind == DISTRIBUTION:
                # What the trust's income was is on its statement; only the TFN withholding,
                # taken from the cash, is not.
                add('13R', row.withheld)
            elif row.dividend_type == DividendType.FOREIGN:
                add('20E', row.cash)
                add('20M', row.cash)
                add('20O', row.foreign_tax_credit)
            else:
                add('11S', row.unfranked)
                add('11T', row.franked)
                add('11U', row.franking_credit)
                add('11V', row.withheld)
                add(LIC_CAPITAL_GAIN, row.lic_capital_gain)

        for line in self.trust_lines:
            if line.fiscal_year != year or not line.counts:
                continue
            if line.label is not None:
                add(line.label, line.amount)
                if line.label == '20M':
                    add('20E', line.amount)
            elif line.component == AttributionComponent.NON_ASSESSABLE_NON_EXEMPT:
                add(NON_ASSESSABLE, line.amount)
        return totals


def _in_account_currency(money: Money | None, rate: 'ExchangeRate | None', currency: str) -> Decimal | None:
    """`money` in `currency`, at the payment's stored rate. None if it has no usable rate."""
    if money is None:
        return ZERO
    if str(money.currency) == currency:
        return Decimal(money.amount)
    if not money.amount:
        return ZERO
    if rate is not None and str(rate.convert_from) == str(money.currency) and str(rate.convert_to) == currency:
        return Decimal(rate.apply(money).amount)
    return None


def _classify(day: date, declared: residency.Periods, foreign_income: bool) -> tuple[str | None, bool, str]:
    """The residency status on `day`, whether income of this kind counts, and why."""
    if not declared:
        return None, True, 'Counted: residency is not declared, so taken as a resident'
    status = residency.status_on(None, day, declared)
    if status == residency.RESIDENT:
        return status, True, 'Counted'
    if status == residency.TEMPORARY:
        if foreign_income:
            return status, False, 'Not counted: foreign income of a temporary resident is not assessable'
        return status, True, 'Counted'
    if status == residency.FOREIGN:
        if foreign_income:
            return status, False, 'Not counted: foreign income of a foreign resident is not assessable'
        return status, False, ('Not counted: Australian income of a foreign resident is taxed by '
                               'withholding, if at all, not on a return')
    return None, False, 'Not counted: no residency is declared for this day'


def _general_warnings(account: 'Account', declared: residency.Periods) -> list[str]:
    """Warnings that apply to every year."""
    warnings: list[str] = []
    if not declared:
        warnings.append('Residency has not been declared, so every payment is counted as an Australian '
                        "resident's. Declare it under Residency periods in the admin.")
    taxpayer_type = discount.taxpayer_type_of(account)
    if taxpayer_type == TaxpayerType.UNDECLARED:
        warnings.append("The account does not say who owns this portfolio. This report is laid out for an "
                        "individual's return.")
    elif taxpayer_type != TaxpayerType.INDIVIDUAL:
        warnings.append(f'This portfolio belongs to a {taxpayer_type.label.lower()}, but this report is laid out '
                        "for an individual's return: the amounts hold, the labels do not.")
    return warnings


def build(account: 'Account') -> IncomeSummary:
    """Every active dividend, distribution and statement income line of the account."""
    from share_dinkum_app.models import AttributionStatement, Distribution, Dividend

    currency = str(account.currency)
    declared = residency.periods(account)
    summary = IncomeSummary()

    def warn(year: str | None, text: str) -> None:
        if year:
            summary.warnings.setdefault(year, []).append(text)

    def describe(kind: str, day: date, instrument: str) -> str:
        return f'The {kind.lower()} from {instrument} paid {day.isoformat()}'

    # (instrument id, instrument name, fiscal year, payment date) of each counted distribution,
    # to check each has a statement.
    distributions: list[tuple[Any, str, 'FiscalYear', date]] = []

    dividends = (Dividend.objects.filter(account=account, is_active=True)
                 .select_related('instrument', 'exchange_rate').order_by('date', 'id'))
    for dividend in dividends:
        foreign = dividend.dividend_type == DividendType.FOREIGN
        status, counts, treatment = _classify(dividend.date, declared, foreign)
        rate = dividend.exchange_rate
        fiscal_year = dividend.fiscal_year
        year = fiscal_year.name if fiscal_year else None
        amounts = {
            'unfranked': _in_account_currency(dividend.total_unfranked_amount, rate, currency),
            'franked': _in_account_currency(dividend.total_franked_amount, rate, currency),
            'franking_credit': _in_account_currency(dividend.total_franking_credits, rate, currency),
            'withheld': _in_account_currency(dividend.local_withholding_tax, rate, currency),
            'foreign_tax_credit': _in_account_currency(dividend.foreign_tax_credit, rate, currency),
            'lic_capital_gain': _in_account_currency(dividend.lic_capital_gain, rate, currency),
        }
        unfranked, franked = amounts['unfranked'], amounts['franked']
        cash = None if unfranked is None or franked is None else unfranked + franked
        summary.payments.append(PaymentRow(
            kind=DIVIDEND, record_id=dividend.pk, legacy_id=dividend.legacy_id, payment_date=dividend.date,
            fiscal_year=year, instrument=dividend.instrument.name, dividend_type=dividend.dividend_type,
            residency=status, counts=counts, treatment=treatment, cash=cash, **amounts))
        if None in amounts.values():
            warn(year, f'{describe(DIVIDEND, dividend.date, dividend.instrument.name)} is in another currency '
                       f'and has no exchange rate to {currency}, so some of it is left out.')
        if declared and status is None:
            warn(year, f'{describe(DIVIDEND, dividend.date, dividend.instrument.name)} falls on a day no '
                       'residency period covers, so it is not counted.')

    payments = (Distribution.objects.filter(account=account, is_active=True)
                .select_related('instrument', 'exchange_rate').order_by('date', 'id'))
    for payment in payments:
        status, counts, treatment = _classify(payment.date, declared, foreign_income=False)
        rate = payment.exchange_rate
        fiscal_year = payment.fiscal_year
        year = fiscal_year.name if fiscal_year else None
        cash = _in_account_currency(payment.total_distribution, rate, currency)
        withheld = _in_account_currency(payment.total_withholding_tax, rate, currency)
        if counts:
            treatment = f'{treatment}, through its annual statement'
        summary.payments.append(PaymentRow(
            kind=DISTRIBUTION, record_id=payment.pk, legacy_id=payment.legacy_id, payment_date=payment.date,
            fiscal_year=year, instrument=payment.instrument.name, dividend_type=None, residency=status,
            counts=counts, treatment=treatment, cash=cash, withheld=withheld))
        if cash is None or withheld is None:
            warn(year, f'{describe(DISTRIBUTION, payment.date, payment.instrument.name)} is in another currency '
                       f'and has no exchange rate to {currency}, so some of it is left out.')
        if declared and status is None:
            warn(year, f'{describe(DISTRIBUTION, payment.date, payment.instrument.name)} falls on a day no '
                       'residency period covers, so it is not counted.')
        if counts and fiscal_year is not None:
            distributions.append((payment.instrument_id, payment.instrument.name, fiscal_year, payment.date))

    have_statement: set[tuple[Any, str]] = set()
    changing_years: dict[str, str] = {}
    statements = (AttributionStatement.objects.filter(account=account, is_active=True)
                  .select_related('instrument').order_by('financial_year_end_date', 'id'))
    for statement in statements:
        fiscal_year = statement.fiscal_year
        year = fiscal_year.name if fiscal_year else None
        if year:
            have_statement.add((statement.instrument_id, year))
        year_end = statement.financial_year_end_date
        if declared and fiscal_year is not None and year:
            # Undeclared days, such as those before the history starts, are not a change: a
            # payment on one is flagged on its own.
            statuses = {status for status in
                        residency.days_by_status(None, fiscal_year.start_date, fiscal_year.end_date, declared)
                        if status is not None}
            if len(statuses) > 1:
                changing_years[year] = str(residency.status_on(None, year_end, declared))

        for row in statement.components.filter(is_active=True).order_by('component'):
            component = row.component
            if component not in TRUST_LABELS and component not in REFERENCE_COMPONENTS:
                continue
            status, counts, treatment = _classify(year_end, declared, component in FOREIGN_COMPONENTS)
            amount = Decimal(row.amount.amount) if str(row.amount.currency) == currency else None
            if amount is None:
                warn(year, f"{statement.instrument.name}'s {year} statement gives "
                           f'{AttributionComponent(component).label.lower()} in {row.amount.currency}, '
                           f'not {currency}, so it is left out. Enter it in {currency}.')
            summary.trust_lines.append(TrustLine(
                statement_legacy_id=statement.legacy_id, fiscal_year=year, year_end=year_end,
                instrument=statement.instrument.name, component=component,
                description=AttributionComponent(component).label, label=TRUST_LABELS.get(component),
                amount=amount, residency=status, counts=counts, treatment=treatment))

    for year, status in changing_years.items():
        warn(year, f'Residency changes during {year}, but an annual statement covers the whole year, so '
                   f'its income is counted under the status on the last day of it ({status}). Apportion it '
                   'by hand if it needs splitting.')

    missing: dict[str, set[str]] = {}
    for instrument_id, name, fiscal_year, paid in distributions:
        if not fiscal_year.name or (instrument_id, fiscal_year.name) in have_statement:
            continue
        previous, _ = account.fiscal_year_type.classify_date(fiscal_year.start_date - timedelta(days=1))
        if paid <= fiscal_year.start_date + LATE_PAYMENT_WINDOW and (instrument_id, previous.name) in have_statement:
            continue
        missing.setdefault(fiscal_year.name, set()).add(name)
    for year, names in missing.items():
        warn(year, f'{", ".join(sorted(names))} paid distributions in {year} with no annual statement for that '
                   'year, so their income is not on this report. Enter the statement under Attribution '
                   'statements in the admin.')

    general = _general_warnings(account, declared)
    for year in summary.years():
        summary.warnings[year] = general + summary.warnings.get(year, [])
    return summary
