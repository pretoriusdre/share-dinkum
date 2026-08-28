from datetime import date
from decimal import Decimal

from djmoney.money import Money

from share_dinkum_app.models import Sell, Account, Parcel, CurrentExchangeRate
import pandas as pd

class RealisedCapitalGainReport:
    def __init__(self, account: Account):
        self.account = account

    def generate(self):
        report_rows = []
        sales_qs = Sell.objects.filter(account=self.account, is_active=True)

        report_columns = [
            "sell_date", "instrument", "quantity_sold", "buy_id", "parcel_id", "sell_id", "sell_allocation_id",
            "buy_date", "days_held", "proceeds", "cost_base", "capital_gain", "fiscal_year"
        ]

        for sell in sales_qs:
            for allocation in sell.sale_allocation.filter(is_active=True):
                parcel = allocation.parcel
                gain = allocation.total_capital_gain
                row = {
                    "sell_date": sell.date,
                    "instrument": sell.instrument.name,
                    "quantity_sold": allocation.quantity,
                    "buy_id": parcel.buy.id,
                    "parcel_id": parcel.id,
                    "sell_id": sell.id,
                    "sell_allocation_id": allocation.id,
                    "buy_date": parcel.buy.date,
                    "days_held" : allocation.days_held,
                    "proceeds": sell.proceeds * (allocation.quantity / sell.quantity),
                    "cost_base": parcel.total_cost_base,
                    "capital_gain": gain,
                    "fiscal_year": allocation.fiscal_year.name if allocation.fiscal_year else None,
                }

                assert list(row.keys()) == report_columns

                report_rows.append(row)

        df = pd.DataFrame(report_rows, columns=report_columns)

        return df

class OpenParcelReport:
    """Summary of every open (unsold) parcel: cost base and current market value."""

    def __init__(self, account: Account):
        self.account = account
        self._rate_cache = {}

    def _to_account_currency(self, money: Money) -> Money:
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
