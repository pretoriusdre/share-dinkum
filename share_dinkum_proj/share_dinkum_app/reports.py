import dataclasses
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from djmoney.money import Money

from share_dinkum_app.models import (
    Account, FiscalYear, CGTReturnSnapshot, CGTReturnSnapshotRow)
import pandas as pd

from share_dinkum_app import cgt, excelinterface, income
from share_dinkum_app.choices import CGTAssetCategory
from share_dinkum_app.cgt.schedule import Schedule


class BaseReport:
    """Base for reports on one portfolio."""

    def __init__(self, account: Account) -> None:
        self.account = account


class UnrealisedGainReport(BaseReport):
    """Every parcel still held, as if sold today, from `cgt.unrealised_events`.

    A guide to what a sale would bring, characterised as a disposal would be, 2027 split and
    indexation included. Not a schedule: no losses are netted, and nothing here goes on a return.
    """

    #: The summary's columns after `asset_category`, in order.
    SUMMARY_FIGURES: list[str] = [
        'cost_base', 'market_value', 'gross_gain', 'gross_loss', 'disregarded_gain', 'discount_if_sold']

    def __init__(self, account: Account, as_at: date | None = None,
                 events: list[cgt.CGTEvent] | None = None) -> None:
        super().__init__(account)
        # Today, not the reporting date: see `cgt.unrealised_events`.
        self.as_at = as_at or date.today()
        self._events = events

    @property
    def events(self) -> list[cgt.CGTEvent]:
        if self._events is None:
            self._events = cgt.unrealised_events(self.account, as_at=self.as_at)
        return self._events

    def generate(self) -> pd.DataFrame:
        """A row per open parcel, or per part of one split at 1 July 2027: every CGTEvent field,
        the date the discount becomes available, and the gain as a fraction of the cost base."""
        columns = [*cgt.event_fields(), 'discount_eligible_from', 'unrealised_gain_pct']
        rows: list[dict[str, Any]] = []
        for event in self.events:
            row = {name: getattr(event, name) for name in cgt.event_fields()}
            row['asset_category'] = CGTAssetCategory.label_for(row['asset_category'])
            row['discount_eligible_from'] = cgt.discount_eligible_from(event)
            cost = getattr(event.cost_base, 'amount', None)
            gain = getattr(event.capital_gain, 'amount', None)
            row['unrealised_gain_pct'] = float(gain / cost) if gain is not None and cost else None
            rows.append(row)
        return pd.DataFrame(rows, columns=columns)

    def summary(self) -> pd.DataFrame:
        """Totals per asset category and overall, as floats.

        The cost base is before indexation, and with the market value is counted once per parcel
        though a split parcel has two rows: the purchase on the part before 1 July 2027, the value
        on the part after it. A parcel with no value (no price or rate) is left out, so cost and
        value cover the same parcels; it is on Warnings instead.
        """
        totals: dict[str, dict[str, Decimal]] = {}
        for event in self.events:
            if event.net_proceeds is None:
                continue
            label = CGTAssetCategory.label_for(event.asset_category)
            line = totals.setdefault(label, {name: Decimal('0') for name in self.SUMMARY_FIGURES})
            if event.slice != cgt.events.SLICE_POST_CUTOVER:
                line['cost_base'] += _amount(event.buy_consideration) + _amount(event.buy_brokerage)
            line['cost_base'] += _amount(event.cost_base_adjustments)
            if event.slice != cgt.events.SLICE_PRE_CUTOVER:
                line['market_value'] += _amount(event.net_proceeds)
            gain = _amount(event.gross_gain)
            line['gross_gain'] += gain
            line['gross_loss'] += _amount(event.gross_loss)
            if event.is_disregarded:
                line['disregarded_gain'] += gain
            else:
                line['discount_if_sold'] += gain * event.discount_percentage

        rows: list[dict[str, Any]] = []
        for label in sorted(totals):
            rows.append({'asset_category': label,
                         **{name: float(value) for name, value in totals[label].items()}})
        if rows:
            rows.append({'asset_category': 'Total', **{
                name: float(sum((line[name] for line in totals.values()), Decimal('0')))
                for name in self.SUMMARY_FIGURES}})
        return pd.DataFrame(rows, columns=['asset_category', *self.SUMMARY_FIGURES])

    def warnings(self) -> list[str]:
        """Each row's pending reason, naming its parcel."""
        return [f'{event.instrument} bought {event.purchase_date}: {event.pending_reason}'
                for event in self.events if event.pending_reason]


def _amount(money: Any) -> Decimal:
    """A Money's amount, or zero for None."""
    return Decimal('0') if money is None else Decimal(money.amount)


class CGTBasisChangeReport:
    """Compare snapshots with a fresh calculation of their year.

    Per event, matched by `cgt.event_key`: each captured field that moved, and events added or
    removed. Both parts of a sale split at 1 July 2027 are compared, and trust gains too. Per
    year: each schedule total that moved, net capital gain included.

    An older snapshot recorded only its disposals' figures, so it is compared on those alone:
    not on how each gain was taxed, not on year totals, and its lack of trust gains is not
    reported.
    """

    #: Compared on every row.
    FIGURE_FIELDS: list[str] = ['quantity_sold', 'days_held', 'proceeds', 'cost_base', 'capital_gain']

    #: Compared only on a row that captured them.
    CHARACTERISATION_FIELDS: list[str] = [
        'method', 'tap_status', 'discount_percentage', 'indexation_factor', 'is_disregarded']

    #: Kept for callers that name the compared fields; every one, in report order.
    COMPARED_FIELDS: list[str] = FIGURE_FIELDS + CHARACTERISATION_FIELDS

    #: Compare at stored money precision, so division residue in older snapshots is not
    #: reported as a change.
    COMPARISON_PLACES = Decimal('0.0001')

    #: Fields stored at another precision, compared at it.
    FIELD_PLACES: dict[str, Decimal] = {
        'discount_percentage': Decimal('0.0000000001'),
        'indexation_factor': Decimal('0.001'),
    }

    #: The `event` of a row about a year total.
    YEAR_TOTAL = 'Year total'

    COLUMNS: list[str] = [
        'fiscal_year', 'taken_at', 'snapshot_basis', 'current_basis', 'snapshot_engine_version',
        'event', 'sell_allocation_id', 'slice', 'status', 'field', 'snapshot_value',
        'current_value', 'difference',
    ]

    def __init__(self, account: Account, fiscal_year: FiscalYear | None = None,
                 lodged_only: bool = False, latest_per_year: bool = False,
                 every_year: list[cgt.CGTEvent] | None = None) -> None:
        self.account = account
        self.fiscal_year = fiscal_year
        self.lodged_only = lodged_only
        #: Compare only one snapshot a year: the latest lodged one, or else the latest.
        self.latest_per_year = latest_per_year
        self._every_year = every_year

    @classmethod
    def _as_decimal(cls, value: Any, places: Decimal | None = None) -> Decimal | None:
        if value is None or value == '':
            return None
        if isinstance(value, Money):
            value = value.amount
        elif not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except (ArithmeticError, ValueError):
                return None
        return value.quantize(places or cls.COMPARISON_PLACES, rounding=ROUND_HALF_UP)

    @classmethod
    def _comparable(cls, field: str, value: Any) -> Any:
        """`value` as it is compared: a number at its field's precision, else as it is."""
        if value is None or value == '':
            return None
        if isinstance(value, (bool, str)):
            return value
        return cls._as_decimal(value, cls.FIELD_PLACES.get(field))

    @staticmethod
    def _key(row: dict[str, Any]) -> str:
        return cgt.event_key(row['source'], row['sell_allocation_id'], row['slice'], row['instrument'],
                             row['sell_date'], row['method'], row['tap_status'])

    @staticmethod
    def _describe(row: dict[str, Any]) -> str:
        """What a row is about, for a person reading the report."""
        if row['source'] == cgt.events.SOURCE_ATTRIBUTION:
            return (f"{row['instrument']} trust gain, year to {row['sell_date']} "
                    f"({row['method']}, {row['tap_status'] or 'TAP unknown'})")
        part = {cgt.events.SLICE_PRE_CUTOVER: ', part before 1 July 2027',
                cgt.events.SLICE_POST_CUTOVER: ', part after 30 June 2027'}.get(row['slice'], '')
        return f"{row['instrument']} sold {row['sell_date']}{part}"

    def _snapshots(self) -> list[CGTReturnSnapshot]:
        snapshots = CGTReturnSnapshot.objects.filter(account=self.account, is_active=True)
        if self.fiscal_year is not None:
            snapshots = snapshots.filter(fiscal_year=self.fiscal_year)
        if self.lodged_only:
            snapshots = snapshots.filter(is_lodged=True)
        chosen = list(snapshots.select_related('fiscal_year').order_by('fiscal_year__start_year', 'taken_at'))
        if not self.latest_per_year:
            return chosen
        by_year: dict[Any, CGTReturnSnapshot] = {}
        for snapshot in chosen:
            kept = by_year.get(snapshot.fiscal_year_id)
            # Ordered by date, so a later one replaces an earlier, unless only the earlier was lodged.
            if kept is None or snapshot.is_lodged or not kept.is_lodged:
                by_year[snapshot.fiscal_year_id] = snapshot
        return list(by_year.values())

    def generate(self) -> pd.DataFrame:
        report_rows: list[dict[str, Any]] = []

        # The basis is a property of the account today, not of the snapshot.
        current_basis = cgt.residency_basis(self.account)
        every_year = self._every_year if self._every_year is not None else cgt.all_events(self.account)
        schedules: dict[str | None, Schedule] = {}

        for snapshot in self._snapshots():
            fiscal_year_name = snapshot.fiscal_year.name
            older = snapshot.is_draft is None
            current_rows = {
                self._key(row): row
                for row in (CGTReturnSnapshotRow.figures_of(event) for event in every_year
                            if event.fiscal_year == fiscal_year_name)
                if not (older and row['source'] != cgt.events.SOURCE_DISPOSAL)
            }
            snapshot_rows = {self._key(row): row for row in snapshot.rows}

            def base_row(row: dict[str, Any] | None, status: str, field: str, was: Any, now: Any,
                         diff: Any) -> dict[str, Any]:
                return {
                    'fiscal_year': fiscal_year_name,
                    'taken_at': snapshot.taken_at,
                    'snapshot_basis': snapshot.basis,
                    'current_basis': current_basis,
                    'snapshot_engine_version': snapshot.engine_version,
                    'event': self.YEAR_TOTAL if row is None else self._describe(row),
                    'sell_allocation_id': None if row is None else row['sell_allocation_id'],
                    'slice': None if row is None else (row['slice'] or None),
                    'status': status,
                    'field': field,
                    'snapshot_value': was,
                    'current_value': now,
                    'difference': diff,
                }

            def compare(row: dict[str, Any] | None, fields: list[str], before: dict[str, Any],
                        after: dict[str, Any]) -> None:
                for field in fields:
                    was = self._comparable(field, before.get(field))
                    now = self._comparable(field, after.get(field))
                    if was == now:
                        continue
                    numbers = isinstance(was, Decimal) and isinstance(now, Decimal)
                    report_rows.append(base_row(
                        row, 'CHANGED', field, was, now, (now - was) if numbers else None))

            for key, snapshot_row in snapshot_rows.items():
                current_row = current_rows.get(key)
                if current_row is None:
                    report_rows.append(base_row(
                        snapshot_row, 'REMOVED', 'capital_gain',
                        self._as_decimal(snapshot_row.get('capital_gain')), None, None))
                    continue
                fields = self.FIGURE_FIELDS if not snapshot_row['source'] else self.COMPARED_FIELDS
                # The current row's own description, so a moved date reads as it is now.
                compare(current_row, fields, snapshot_row, current_row)

            for key, current_row in current_rows.items():
                if key not in snapshot_rows:
                    report_rows.append(base_row(
                        current_row, 'ADDED', 'capital_gain',
                        None, self._as_decimal(current_row.get('capital_gain')), None))

            if not older:
                if fiscal_year_name not in schedules:
                    schedules[fiscal_year_name] = cgt.build_schedule(
                        self.account, snapshot.fiscal_year, every_year=every_year)
                schedule = schedules[fiscal_year_name]
                now_totals = {name: getattr(schedule, attribute)
                              for name, attribute in CGTReturnSnapshot.SCHEDULE_FIELDS.items()}
                compare(None, list(CGTReturnSnapshot.SCHEDULE_FIELDS), snapshot.schedule_figures, now_totals)

        df = pd.DataFrame(report_rows, columns=self.COLUMNS)
        if not df.empty:
            df = df.sort_values(['fiscal_year', 'taken_at', 'event', 'field'], ignore_index=True)
        return df


class CGTEventReport(BaseReport):
    """Every capital gains event, including trust attributions, with every CGTEvent field.

    Asset categories are shown as their ATO labels.
    """

    def __init__(self, account: Account, fiscal_year: FiscalYear | str | None = None,
                 every_year: list[cgt.CGTEvent] | None = None) -> None:
        super().__init__(account)
        self.fiscal_year = fiscal_year
        #: Every year's events if the caller has them already, which saves working them out again.
        self.every_year = every_year

    def generate(self) -> pd.DataFrame:
        columns = cgt.event_fields()
        rows: list[dict[str, Any]] = []
        events = (cgt.all_events(self.account, fiscal_year=self.fiscal_year)
                  if self.every_year is None else _in_year(self.every_year, self.fiscal_year))
        for event in events:
            row = {name: getattr(event, name) for name in columns}
            # The category is stored as a stable code and read as the ATO's wording. This
            # is the boundary between the two: a person filling in a schedule is looking
            # for "Shares in Australian listed companies", not AU_LISTED_SHARES.
            row['asset_category'] = CGTAssetCategory.label_for(row['asset_category'])
            rows.append(row)
        return pd.DataFrame(rows, columns=columns)


class CGTScheduleReport(BaseReport):
    """The s102-5 method statement for a fiscal year, one row per s102-6 category.

    A draft while `warnings()` is non-empty.
    """

    def __init__(self, account: Account, fiscal_year: FiscalYear | str | None = None,
                 every_year: list[cgt.CGTEvent] | None = None) -> None:
        super().__init__(account)
        self.fiscal_year = fiscal_year
        self.every_year = every_year
        self._schedule: Schedule | None = None

    @property
    def schedule(self) -> Schedule:
        if self._schedule is None:
            self._schedule = cgt.build_schedule(self.account, self.fiscal_year, every_year=self.every_year)
        return self._schedule

    def warnings(self) -> list[str]:
        return list(self.schedule.warnings)

    @property
    def is_draft(self) -> bool:
        return self.schedule.is_draft

    def generate(self) -> pd.DataFrame:
        columns = [
            "category", "gross_gains", "current_year_losses_applied",
            "prior_year_losses_applied", "gain_before_discount", "discount_applied",
            "net_gain",
        ]
        rows = [
            {name: getattr(line, name) for name in columns}
            for line in self.schedule.lines
        ]
        return pd.DataFrame(rows, columns=columns)

    def summary(self) -> dict[str, Any]:
        """The year's totals, draft flag and warnings, as a dict."""
        schedule = self.schedule
        return {
            'fiscal_year': schedule.fiscal_year,
            'basis': schedule.basis,
            'total_current_year_capital_gains': schedule.gross_gains,
            'total_current_year_capital_losses': schedule.gross_losses,
            'disregarded_capital_gains': schedule.disregarded_gains,
            'losses_applied': schedule.current_year_losses_applied,
            'prior_year_losses_applied': schedule.prior_year_losses_applied,
            'cgt_discount_applied': schedule.total_discount,
            'net_capital_gain': schedule.net_capital_gain,
            'losses_carried_forward': schedule.losses_carried_forward,
            'minimum_tax_capital_gain_base': schedule.minimum_tax_capital_gain_base,
            'is_draft': schedule.is_draft,
            'warnings': list(schedule.warnings),
        }


def _in_year(events: list[cgt.CGTEvent], fiscal_year: FiscalYear | str | None) -> list[cgt.CGTEvent]:
    """`events` narrowed to one fiscal year (a FiscalYear or its name), or all if None."""
    wanted = getattr(fiscal_year, 'name', fiscal_year)
    return [e for e in events if wanted is None or e.fiscal_year == wanted]


def _plain(value: Any) -> float | None:
    """A Money (or number) as a float, so Excel can sum it. None stays None."""
    if value is None:
        return None
    amount = getattr(value, 'amount', value)
    return float(amount)


def cgt_schedule_workbook(account: Account, output_path: str | Path, fiscal_years: list[str] | None = None) -> str | Path:
    """Write the CGT schedule workbook to `output_path` and return the path.

    Covers `fiscal_years`, default every year with a sale or a trust's annual statement.
    Draft years are included, with an `is_draft` column and a Warnings sheet.
    """
    if fiscal_years is None:
        fiscal_years = [year.name for year in cgt.years_with_events(account) if year.name]

    # Worked out once: each year's schedule, the return frame and the events sheet all need it.
    every_year = cgt.all_events(account)
    reports: dict[str, CGTScheduleReport] = {}

    summaries: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for year in fiscal_years:
        report = reports[year] = CGTScheduleReport(
            account=account, fiscal_year=year, every_year=every_year)
        summary = report.summary()

        summaries.append({
            'fiscal_year': year,
            'is_draft': summary['is_draft'],
            'basis': summary['basis'],
            'total_current_year_capital_gains': _plain(summary['total_current_year_capital_gains']),
            'total_current_year_capital_losses': _plain(summary['total_current_year_capital_losses']),
            'disregarded_capital_gains': _plain(summary['disregarded_capital_gains']),
            'losses_applied': _plain(summary['losses_applied']),
            'prior_year_losses_applied': _plain(summary['prior_year_losses_applied']),
            'cgt_discount_applied': _plain(summary['cgt_discount_applied']),
            'net_capital_gain': _plain(summary['net_capital_gain']),
            'losses_carried_forward': _plain(summary['losses_carried_forward']),
            'minimum_tax_capital_gain_base': _plain(summary['minimum_tax_capital_gain_base']),
        })

        for _, row in report.generate().iterrows():
            line: dict[str, Any] = {'fiscal_year': year, 'is_draft': summary['is_draft']}
            line.update({
                str(name): value if name == 'category' else _plain(value)
                for name, value in row.items()
            })
            lines.append(line)

        for warning in summary['warnings']:
            warnings.append({'fiscal_year': year, 'warning': warning})

    events = _flattened(CGTEventReport(account=account, every_year=every_year).generate())

    changes = _flattened(CGTBasisChangeReport(
        account=account, latest_per_year=True, every_year=every_year).generate())

    unrealised = UnrealisedGainReport(account=account)
    warnings += [{'fiscal_year': 'Unrealised', 'warning': text} for text in unrealised.warnings()]

    generator = excelinterface.ExcelGen(title='Capital Gains Tax Schedule')
    generator.add_table(
        cgt_return_schedule_frame(account, fiscal_years, reports, every_year),
        table_name='ReturnSchedule', add_hyperlinks=False,
        description=('The ATO capital gains schedule as the form lays it out: one row per '
                     'label, one column per year, to be read across rather than assembled.'))
    generator.add_table(
        pd.DataFrame(summaries), table_name='Summary', add_hyperlinks=False,
        description='The s102-5 figures a return asks for, one row per fiscal year.')
    generator.add_table(
        pd.DataFrame(lines), table_name='ScheduleLines', add_hyperlinks=False,
        description='Each s102-6 category, per year: gains, losses applied, discount, net.')
    generator.add_table(
        events, table_name='Events', add_hyperlinks=False,
        description='Every capital gains event, with how it was characterised and why.')
    generator.add_table(
        changes, table_name='ChangesSinceSnapshot', add_hyperlinks=False,
        description=("What has moved since each year's capital gains snapshot: the latest one marked "
                     'lodged, or else the latest. A year total, or one event and one field, per row. '
                     'Empty if nothing moved or no snapshot has been taken.'))
    generator.add_table(
        _flattened(unrealised.generate()), table_name='UnrealisedGains', add_hyperlinks=False,
        description=(f'Every parcel still held, as if sold on {unrealised.as_at.isoformat()} at the latest '
                     'price with no brokerage, and characterised as that sale would be, split at 1 July '
                     '2027 where a real sale would be. A guide to planning a sale; nothing here goes on '
                     'a return.'))
    generator.add_table(
        unrealised.summary(), table_name='UnrealisedSummary', add_hyperlinks=False,
        description=('The unrealised gains by asset category. Losses are not netted against gains, so '
                     'the discount shown is the most a sale of everything could get. A parcel with no '
                     'price or exchange rate is left out, and listed on Warnings.'))
    generator.add_table(
        pd.DataFrame(warnings, columns=['fiscal_year', 'warning']),
        table_name='Warnings', add_hyperlinks=False,
        description=('Why a year is still a draft. Empty means every year is final. "Unrealised" rows '
                     'are about the UnrealisedGains sheet and leave every year final.'))
    generator.save(output_path)
    return output_path


def _flattened(df: pd.DataFrame) -> pd.DataFrame:
    """`df` with each Money as a float, so Excel can sum it."""
    for column in df.columns:
        df[column] = df[column].map(lambda value: _plain(value) if hasattr(value, 'amount') else value)
    return df


#: The ATO schedule's lines in form order, as `(kind, label, figure key)`. A None key is a
#: heading; `flag` marks a figure to check rather than copy.
CGT_RETURN_LAYOUT: list[tuple[str, str, str | None]] = [
    ('heading', 'Current year capital gains and losses', None),
    *[entry for category in CGTAssetCategory.reportable() for entry in (
        ('subheading', category.label, None),
        ('row', '    Capital gain', f'gain::{category.value}'),
        ('row', '    Capital loss', f'loss::{category.value}'),
    )],
    ('row', 'Capital gains from trusts (including managed funds)', 'trust_gains'),
    # Not boxes on the form, and here for exactly that reason. An unclassified gain or loss
    # belongs to none of the eight, so without a line of its own it would drop out of this
    # sheet while still counting towards the totals below -- a schedule that does not foot,
    # with nothing on it saying why. They read zero once everything is classified.
    ('flag', 'Unclassified capital gain (not reportable until the holding is classified)',
     'unclassified'),
    ('flag', 'Unclassified capital loss (not reportable until the holding is classified)',
     'unclassified_loss'),
    ('total', 'Total current year capital gains', 'total_gains'),
    ('total', 'Total current year capital losses', 'total_losses'),

    ('heading', 'Capital losses applied', None),
    ('row', '    Total current year capital losses applied', 'applied_current'),
    ('row', '    Total prior year net capital losses applied', 'applied_prior'),
    ('total', 'Total capital losses applied', 'applied_total'),

    ('heading', 'Unapplied net capital losses carried forward', None),
    ('total', 'Net capital losses carried forward', 'carried_forward'),

    ('heading', 'CGT discount', None),
    ('total', 'Total CGT discount applied', 'discount_applied'),

    ('heading', 'Other CGT information', None),
    ('flag', 'Capital gains disregarded by a foreign resident', 'disregarded'),

    ('heading', 'Other fields', None),
    ('total', 'Net capital gain', 'net_capital_gain'),
    ('row', 'Net capital loss carried forward to later income years', 'carried_forward'),
]


def _cgt_return_figures(account: Account, fiscal_year: str,
                        report: CGTScheduleReport | None = None,
                        every_year: list[cgt.CGTEvent] | None = None,
                        ) -> tuple[dict[str, float | None], bool]:
    """One year's figures keyed to CGT_RETURN_LAYOUT, and whether the year is a draft.

    Per-asset-category gains and losses come from the events, since `Schedule.lines` groups
    by s102-6 category instead. Trust attributions go on their own line.
    """
    if report is None:
        report = CGTScheduleReport(account=account, fiscal_year=fiscal_year, every_year=every_year)
    schedule = report.schedule

    gains: dict[str, Decimal] = {}
    losses: dict[str, Decimal] = {}
    trust_gains = Decimal('0')
    year_events = (cgt.all_events(account, fiscal_year=fiscal_year) if every_year is None
                   else _in_year(every_year, fiscal_year))
    for event in year_events:
        if event.is_disregarded:
            continue
        gain = Decimal(str(getattr(event.gross_gain, 'amount', 0) or 0))
        loss = Decimal(str(getattr(event.gross_loss, 'amount', 0) or 0))
        if event.source == cgt.events.SOURCE_ATTRIBUTION:
            trust_gains += gain
            continue
        category = event.asset_category
        gains[category] = gains.get(category, Decimal('0')) + gain
        losses[category] = losses.get(category, Decimal('0')) + loss

    figures: dict[str, float | None] = {
        'trust_gains': float(trust_gains),
        'total_gains': _plain(schedule.gross_gains),
        'total_losses': _plain(schedule.gross_losses),
        'applied_current': _plain(schedule.current_year_losses_applied),
        'applied_prior': _plain(schedule.prior_year_losses_applied),
        'applied_total': (_plain(schedule.current_year_losses_applied) or 0)
                         + (_plain(schedule.prior_year_losses_applied) or 0),
        'carried_forward': _plain(schedule.losses_carried_forward),
        'discount_applied': _plain(schedule.total_discount),
        'disregarded': _plain(schedule.disregarded_gains),
        'net_capital_gain': _plain(schedule.net_capital_gain),
    }
    for category in CGTAssetCategory.reportable():
        # Keyed by the stored code, because that is what a CGTEvent carries. Only
        # CGTEventReport swaps the code for the ATO's wording, and it does that on its way
        # out to a spreadsheet -- reading these off the events means reading codes.
        figures[f'gain::{category.value}'] = float(gains.get(category.value, Decimal('0')))
        figures[f'loss::{category.value}'] = float(losses.get(category.value, Decimal('0')))

    # Whatever the eight boxes do not account for. Summed as a remainder rather than read
    # off the UNCLASSIFIED key alone, so that a category added to the enum and forgotten
    # here still shows up somewhere instead of quietly leaving the sheet short.
    reportable = {c.value for c in CGTAssetCategory.reportable()}
    figures['unclassified'] = float(
        sum(amount for key, amount in gains.items() if key not in reportable))
    figures['unclassified_loss'] = float(
        sum(amount for key, amount in losses.items() if key not in reportable))
    return figures, schedule.is_draft


def cgt_return_schedule_frame(account: Account, fiscal_years: list[str],
                              reports: dict[str, CGTScheduleReport] | None = None,
                              every_year: list[cgt.CGTEvent] | None = None) -> pd.DataFrame:
    """The schedule as the form lays it out: a row per line, a column per year, plus a draft row."""
    per_year: dict[str, dict[str, float | None]] = {}
    drafts: dict[str, bool] = {}
    for year in fiscal_years:
        per_year[year], drafts[year] = _cgt_return_figures(
            account, year, (reports or {}).get(year), every_year)

    rows: list[dict[str, Any]] = []
    for kind, label, key in CGT_RETURN_LAYOUT:
        row: dict[str, Any] = {'line': label, 'kind': kind}
        for year in fiscal_years:
            row[year] = None if key is None else per_year[year].get(key)
        rows.append(row)

    # So the reader knows which columns are still moving without leaving the sheet.
    status: dict[str, Any] = {'line': 'Draft (year not final)', 'kind': 'flag'}
    status.update({year: 'yes' if drafts[year] else 'no' for year in fiscal_years})
    rows.append(status)

    return pd.DataFrame(rows, columns=['line', 'kind', *fiscal_years])


#: The income section of an individual's return in form order, as `(kind, label, figure key)`,
#: in the shape of CGT_RETURN_LAYOUT. A None key is a heading, or a line with nothing to copy.
INCOME_RETURN_LAYOUT: list[tuple[str, str, str | None]] = [
    ('heading', 'Dividends (item 11)', None),
    *[('row', f'    {key} {income.LABELS[key]}', key) for key in ('11S', '11T', '11U', '11V')],
    ('heading', 'Partnerships and trusts (item 13)', None),
    *[('row', f'    {key} {income.LABELS[key]}', key) for key in ('13U', '13C', '13Q', '13R', '13A')],
    ('heading', 'Capital gains (item 18)', None),
    ('flag', '    Including gains from trusts: see the Australian CGT report', None),
    ('heading', 'Foreign source income (item 20)', None),
    *[('row', f'    {key} {income.LABELS[key]}', key) for key in ('20E', '20M', '20O')],
    ('heading', 'For reference, not copied to a label', None),
    ('flag', '    LIC capital gain amount (a deduction for part of it may be claimable)', income.LIC_CAPITAL_GAIN),
    ('flag', '    Non-assessable non-exempt amounts from trusts', income.NON_ASSESSABLE),
    ('flag', '    Payments not counted (see NonResident)', income.EXCLUDED_CASH),
    ('flag', '    Tax withheld from payments not counted', income.EXCLUDED_WITHHELD),
]


class IncomeReport(BaseReport):
    """Dividends and trust income by return label, for each fiscal year. See `income`."""

    def __init__(self, account: Account) -> None:
        super().__init__(account)
        self._summary: income.IncomeSummary | None = None

    @property
    def summary(self) -> income.IncomeSummary:
        if self._summary is None:
            self._summary = income.build(self.account)
        return self._summary

    def generate(self) -> pd.DataFrame:
        """Every payment, with its residency, whether it counts, and its amounts."""
        return _rows_frame(self.summary.payments, income.PaymentRow)

    def trust_lines(self) -> pd.DataFrame:
        return _rows_frame(self.summary.trust_lines, income.TrustLine)

    def return_schedule(self, fiscal_years: list[str]) -> pd.DataFrame:
        """A row per line of INCOME_RETURN_LAYOUT, a column per year, plus a draft row."""
        figures = {year: self.summary.figures(year) for year in fiscal_years}
        rows: list[dict[str, Any]] = []
        for kind, label, key in INCOME_RETURN_LAYOUT:
            row: dict[str, Any] = {'line': label, 'kind': kind}
            for year in fiscal_years:
                row[year] = None if key is None else _plain(figures[year][key])
            rows.append(row)
        status: dict[str, Any] = {'line': 'Draft (year not final)', 'kind': 'flag'}
        status.update({year: 'yes' if self.summary.is_draft(year) else 'no' for year in fiscal_years})
        rows.append(status)
        return pd.DataFrame(rows, columns=['line', 'kind', *fiscal_years])


def _rows_frame(rows: list[Any], row_type: type) -> pd.DataFrame:
    """Dataclass rows as a frame, Decimals as floats and ids as text, so Excel can use them."""
    columns = [column.name for column in dataclasses.fields(row_type)]
    records = []
    for row in rows:
        record = {}
        for name in columns:
            value = getattr(row, name)
            if isinstance(value, Decimal):
                value = float(value)
            elif name == 'record_id' and value is not None:
                value = str(value)
            record[name] = value
        records.append(record)
    return pd.DataFrame(records, columns=columns)


def income_workbook(account: Account, output_path: str | Path, fiscal_years: list[str] | None = None) -> str | Path:
    """Write the income report workbook to `output_path` and return the path.

    Covers `fiscal_years`, default every year with a payment or an annual statement. Draft years
    are included, with a draft row and a Warnings sheet.
    """
    report = IncomeReport(account=account)
    summary = report.summary
    if fiscal_years is None:
        fiscal_years = summary.years()

    payments = report.generate()
    trust_lines = report.trust_lines()
    if not payments.empty:
        payments = payments[payments['fiscal_year'].isin(fiscal_years)]
    if not trust_lines.empty:
        trust_lines = trust_lines[trust_lines['fiscal_year'].isin(fiscal_years)]
    excluded = payments[~payments['counts'].astype(bool)] if not payments.empty else payments
    warnings = [{'fiscal_year': year, 'warning': text}
                for year in fiscal_years for text in summary.year_warnings(year)]

    generator = excelinterface.ExcelGen(title='Australian Income Report')
    generator.add_table(
        report.return_schedule(fiscal_years), table_name='ReturnSchedule', add_hyperlinks=False,
        description=("The income section of an individual's return: one row per label, one column per "
                     'year. Capital gains are on the CGT report.'))
    generator.add_table(
        payments, table_name='Payments', add_hyperlinks=False,
        description=('Every dividend and distribution, in the portfolio currency at the rate on the day it '
                     "was paid, with the residency it was paid under and whether it counts. A trust's "
                     'income counts through its annual statement, not its cash.'))
    generator.add_table(
        trust_lines, table_name='TrustIncome', add_hyperlinks=False,
        description="The income lines of each trust's annual statement, and the label each goes on.")
    generator.add_table(
        excluded, table_name='NonResident', add_hyperlinks=False,
        description=('Payments not counted: paid while a foreign resident, foreign income of a temporary '
                     'resident, or on a day no residency is declared for.'))
    generator.add_table(
        pd.DataFrame(warnings, columns=['fiscal_year', 'warning']), table_name='Warnings', add_hyperlinks=False,
        description='Why a year is still a draft. Empty means every year is final.')
    generator.save(output_path)
    return output_path
