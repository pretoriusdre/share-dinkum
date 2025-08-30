from share_dinkum_app.models import Sell, Account
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
                    "buy_id": parcel.id,
                    "parcel_id": parcel.buy.id,
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