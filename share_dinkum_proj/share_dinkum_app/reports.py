from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from djmoney.money import Money

from share_dinkum_app.models import Sell, Account, Parcel, CurrentExchangeRate, CGTReturnSnapshot
import pandas as pd

from share_dinkum_app import cgt
from share_dinkum_app.choices import CGTAssetCategory, CGTBasis

def _key(value):
    """A sell allocation id as text, or None.

    Both sides of the comparison have to agree on the type of the key. They have not always:
    the captured figures used to come back from JSON as strings and now come back as real
    UUIDs, at which point nothing matched and every row was reported as removed and re-added.
    """
    return None if value is None else str(value)


class BaseReport:
    """Shared machinery for reports that have to talk about money in one currency.

    A portfolio can hold instruments quoted in several currencies, and a total that mixes
    them is meaningless. Conversion was written once inside OpenParcelReport and needed by
    every report since, so it lives here rather than being copied with small differences.
    """

    def __init__(self, account: Account):
        self.account = account
        self._rate_cache = {}

    def _to_account_currency(self, money: Money) -> Money:
        if money is None:
            return money
        if str(money.currency) == str(self.account.currency):
            return money

        rate = self._rate_cache.get(str(money.currency))
        if rate is None:
            rate = CurrentExchangeRate.get_or_create(
                account=self.account,
                convert_from=money.currency,
                convert_to=self.account.currency,
            )
            if not rate:
                raise ValueError(f'No exchange rate available for {money.currency} to {self.account.currency}')
            self._rate_cache[str(money.currency)] = rate

        return rate.apply(money)


class RealisedCapitalGainReport:
    """One row per parcel consumed by a sale.

    The column set is deliberately fixed. This report is what users have been reading and
    exporting for years, so it is not the place to add a discount or a category as those
    arrive -- new columns belong on a new report, where a change in shape cannot disturb an
    existing one.

    The figures come from the cgt package rather than being recomputed here, so that this
    report and every later one agree by construction.
    """

    def __init__(self, account: Account):
        self.account = account

    def generate(self):
        report_columns = [
            "sell_date", "instrument", "quantity_sold", "buy_id", "parcel_id", "sell_id", "sell_allocation_id",
            "buy_date", "days_held", "proceeds", "cost_base", "capital_gain", "fiscal_year"
        ]

        report_rows = [
            {
                "sell_date": event.event_date,
                "instrument": event.instrument,
                "quantity_sold": event.quantity,
                "buy_id": event.buy_id,
                "parcel_id": event.parcel_id,
                "sell_id": event.sell_id,
                "sell_allocation_id": event.sell_allocation_id,
                "buy_date": event.purchase_date,
                "days_held": event.days_held,
                "proceeds": event.net_proceeds,
                "cost_base": event.cost_base,
                "capital_gain": event.capital_gain,
                "fiscal_year": event.fiscal_year,
            }
            for event in cgt.disposal_events(self.account)
        ]

        return pd.DataFrame(report_rows, columns=report_columns)

class OpenParcelReport(BaseReport):
    """Summary of every open (unsold) parcel: cost base and current market value."""

    def generate(self):
        report_columns = [
            "instrument", "parcel_id", "buy_id", "buy_date", "days_held",
            "remaining_quantity", "unit_cost_base", "cost_base",
            "instrument_currency", "current_unit_price", "current_value",
            "unrealised_gain", "unrealised_gain_pct",
        ]

        today = date.today()
        report_rows = []

        parcels = (
            Parcel.objects.filter(account=self.account, is_active=True)
            .select_related('buy', 'buy__instrument')
        )

        for parcel in parcels:
            remaining_quantity = parcel.remaining_quantity
            if remaining_quantity <= Decimal('0'):
                continue

            instrument = parcel.buy.instrument
            unit_cost_base = parcel.unit_cost_base
            cost_base = unit_cost_base * remaining_quantity

            current_unit_price = instrument.current_unit_price
            if current_unit_price is None:
                current_value = None
                unrealised_gain = None
                unrealised_gain_pct = None
            else:
                current_value = self._to_account_currency(
                    Money(current_unit_price * remaining_quantity, instrument.currency)
                )
                unrealised_gain = current_value - cost_base
                unrealised_gain_pct = (
                    float(unrealised_gain.amount / cost_base.amount) if cost_base.amount else None
                )

            report_rows.append({
                "instrument": instrument.name,
                "parcel_id": parcel.id,
                "buy_id": parcel.buy.id,
                "buy_date": parcel.buy.date,
                "days_held": (today - parcel.buy.date).days,
                "remaining_quantity": remaining_quantity,
                "unit_cost_base": unit_cost_base,
                "cost_base": cost_base,
                "instrument_currency": str(instrument.currency),
                "current_unit_price": current_unit_price,
                "current_value": current_value,
                "unrealised_gain": unrealised_gain,
                "unrealised_gain_pct": unrealised_gain_pct,
            })

        df = pd.DataFrame(report_rows, columns=report_columns)

        return df.sort_values(["instrument", "buy_date"], ignore_index=True) if not df.empty else df


class CGTBasisChangeReport:
    """Compares captured capital gains figures against a fresh calculation.

    Capital gains figures are derived rather than stored, so a correction anywhere in the
    calculation changes what the app reports for years already lodged. This report makes
    that movement explicit: for every snapshot, it recomputes the year and reports each
    line that changed, was added, or disappeared.

    A row here is not necessarily a problem. It usually means a figure has become more
    correct. The point is that the change is visible and attributable, rather than a number
    quietly differing from the one on a lodged return.
    """

    #: Fields worth comparing. Identifiers and dates are used to match rows, not diffed.
    COMPARED_FIELDS = ['quantity_sold', 'days_held', 'proceeds', 'cost_base', 'capital_gain']

    def __init__(self, account: Account, fiscal_year=None, lodged_only: bool = False):
        self.account = account
        self.fiscal_year = fiscal_year
        self.lodged_only = lodged_only

    #: Figures are compared at the precision the application stores money to. Anything
    #: finer is the residue of an inexact division rather than a real difference, and a
    #: snapshot taken under an older build can carry a full 28 significant digits of it.
    #: Comparing raw would report every row of every old snapshot as changed, by a
    #: hundred-thousandth of a cent, and bury the changes that matter.
    COMPARISON_PLACES = Decimal('0.0001')

    @classmethod
    def _as_decimal(cls, value):
        if value is None or value == '':
            return None
        if isinstance(value, Money):
            value = value.amount
        elif not isinstance(value, Decimal):
            try:
                value = Decimal(str(value))
            except (ArithmeticError, ValueError):
                return None
        return value.quantize(cls.COMPARISON_PLACES, rounding=ROUND_HALF_UP)

    def _current_rows_by_key(self, fiscal_year_name):
        """Current figures for one fiscal year, keyed by sell allocation."""
        df = RealisedCapitalGainReport(account=self.account).generate()
        if df.empty:
            return {}
        df = df[df['fiscal_year'] == fiscal_year_name]
        return {_key(row['sell_allocation_id']): row for _, row in df.iterrows()}

    def generate(self):
        report_columns = [
            'fiscal_year', 'taken_at', 'snapshot_basis', 'current_basis',
            'snapshot_engine_version', 'sell_allocation_id', 'status', 'field',
            'snapshot_value', 'current_value', 'difference',
        ]
        report_rows = []

        snapshots = CGTReturnSnapshot.objects.filter(account=self.account, is_active=True)
        if self.fiscal_year is not None:
            snapshots = snapshots.filter(fiscal_year=self.fiscal_year)
        if self.lodged_only:
            snapshots = snapshots.filter(is_lodged=True)

        for snapshot in snapshots.select_related('fiscal_year'):
            fiscal_year_name = snapshot.fiscal_year.name
            current_rows = self._current_rows_by_key(fiscal_year_name)
            # Keyed as text on both sides. The snapshot holds a real UUID and the report
            # a UUID too, but they arrive through pandas, and matching on the string form
            # is the one thing that cannot be quietly wrong about a type.
            snapshot_rows = {
                _key(row.get('sell_allocation_id')): row for row in snapshot.rows}

            # The basis is a property of the account today, not of the snapshot; until
            # ResidencyPeriod exists every account computes on the legacy basis.
            current_basis = CGTBasis.LEGACY

            def base_row(allocation_id, status, field, was, now, diff):
                return {
                    'fiscal_year': fiscal_year_name,
                    'taken_at': snapshot.taken_at,
                    'snapshot_basis': snapshot.basis,
                    'current_basis': current_basis,
                    'snapshot_engine_version': snapshot.engine_version,
                    'sell_allocation_id': allocation_id,
                    'status': status,
                    'field': field,
                    'snapshot_value': was,
                    'current_value': now,
                    'difference': diff,
                }

            for allocation_id, snapshot_row in snapshot_rows.items():
                current_row = current_rows.get(allocation_id)
                if current_row is None:
                    report_rows.append(base_row(
                        allocation_id, 'REMOVED', 'capital_gain',
                        snapshot_row.get('capital_gain'), None, None))
                    continue

                for field in self.COMPARED_FIELDS:
                    was = self._as_decimal(snapshot_row.get(field))
                    now = self._as_decimal(current_row.get(field))
                    if was is None and now is None:
                        continue
                    if was is not None and now is not None and was == now:
                        continue
                    difference = (now - was) if (was is not None and now is not None) else None
                    report_rows.append(base_row(
                        allocation_id, 'CHANGED', field, was, now, difference))

            for allocation_id, current_row in current_rows.items():
                if allocation_id not in snapshot_rows:
                    report_rows.append(base_row(
                        allocation_id, 'ADDED', 'capital_gain',
                        None, self._as_decimal(current_row.get('capital_gain')), None))

        df = pd.DataFrame(report_rows, columns=report_columns)
        if not df.empty:
            df = df.sort_values(
                ['fiscal_year', 'taken_at', 'sell_allocation_id', 'field'],
                ignore_index=True,
            )
        return df


class CGTEventReport(BaseReport):
    """Every capital gains event for an account, with its full characterisation.

    Where RealisedCapitalGainReport is frozen at the columns it has always had, this one is
    free to grow: it emits every field of CGTEvent, in declaration order, so a field added
    to the fact table appears here without anything being edited. That is the whole reason
    the two are separate reports rather than one report with a flag.

    Unlike the realised gains report it also carries trust attributions, and from 1 July 2027
    it carries two rows for a disposal the deemed sale splits, sharing one sell allocation
    id.
    """

    def __init__(self, account: Account, fiscal_year=None):
        super().__init__(account)
        self.fiscal_year = fiscal_year

    def generate(self):
        columns = cgt.event_fields()
        rows = []
        for event in cgt.all_events(self.account, fiscal_year=self.fiscal_year):
            row = {name: getattr(event, name) for name in columns}
            # The category is stored as a stable code and read as the ATO's wording. This
            # is the boundary between the two: a person filling in a schedule is looking
            # for "Shares in Australian listed companies", not AU_LISTED_SHARES.
            row['asset_category'] = CGTAssetCategory.label_for(row['asset_category'])
            rows.append(row)
        return pd.DataFrame(rows, columns=columns)


class CGTScheduleReport(BaseReport):
    """The s102-5 method statement for a fiscal year, one row per s102-6 category.

    **Refuses to present itself as final while anything is unconfirmed.** An unclassified
    instrument, a suggested rather than confirmed asset category, an undeclared residency, a
    trust statement that does not reconcile, or a missing valuation each make this a draft,
    and `warnings()` says which. Producing a confident schedule over incomplete data is the
    failure this guards against: the number looks the same either way, and only one of them
    can be lodged.
    """

    def __init__(self, account: Account, fiscal_year=None):
        super().__init__(account)
        self.fiscal_year = fiscal_year
        self._schedule = None

    @property
    def schedule(self):
        if self._schedule is None:
            self._schedule = cgt.build_schedule(self.account, self.fiscal_year)
        return self._schedule

    def warnings(self):
        return list(self.schedule.warnings)

    @property
    def is_draft(self):
        return self.schedule.is_draft

    def generate(self):
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

    def summary(self):
        """The single figures a return actually asks for, plus what qualifies them."""
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
