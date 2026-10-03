"""Fill an import template with a believable fake portfolio, for the sample data.

Prices, splits and per-share dividends are Yahoo's real history, so incomes are in line with what
the holdings really paid. Everything else (what was bought, when, how much) is invented from a
seeded random generator, so the same seed and the same market data give the same file. Every
populated table's first row is marked as fake in `notes`.

The shape of the portfolio is the block of constants below: change them to change the portfolio.
Needs network access, unless `--cache` points at market data saved by an earlier run.
"""

import datetime as dt
import math
import pickle
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import openpyxl
import pandas as pd

from django.core.management.base import BaseCommand, CommandError, CommandParser

from share_dinkum_app import excelinterface
from share_dinkum_app.management.commands import make_import_template


FAKE_NOTE = 'FAKE DATA - generated for demonstration, not a real portfolio'
MANUAL_NOTE = ('Strategy is MANUAL, so the SellAllocation table must be populated with the parcels '
               'this sale consumes.')
ALLOCATION_NOTE = ('lookup_legacy_sell and lookup_legacy_buy must match the legacy_id of the Sell '
                   'and Buy rows.')

# An example of what goes in a `file` cell, on the first row of these tables. Loading it logs that the
# file does not exist and stores nothing, so it is harmless, and shows where a real path would go.
EXAMPLE_FILES = {
    'Buy': r'C:\full_path_to_file\buy-001.pdf',
    'Sell': r'C:\full_path_to_file\sell-001.pdf',
}

DEFAULT_SEED = 2026
START = dt.date(2016, 10, 1)


@dataclass(frozen=True)
class Holding:
    currency: str
    market: str
    description: str
    ticker: str
    # Days from the ex-date to the payment, which is the date income is recorded on.
    pay_lag_days: int = 14

    @property
    def is_usd(self) -> bool:
        return self.currency == 'USD'


HOLDINGS: dict[str, Holding] = {
    'VGS': Holding('AUD', 'ASX', 'Vanguard MSCI INDEX International Shares ETF', 'VGS.AX', 14),
    'A200': Holding('AUD', 'ASX', 'Betashares Australia 200 ETF', 'A200.AX', 14),
    'VAS': Holding('AUD', 'ASX', 'Vanguard Australian Shares INDEX ETF', 'VAS.AX', 14),
    'CBA': Holding('AUD', 'ASX', 'Commonwealth Bank of Australia', 'CBA.AX', 35),
    'BHP': Holding('AUD', 'ASX', 'BHP Group Ltd', 'BHP.AX', 21),
    'TLS': Holding('AUD', 'ASX', 'Telstra Group Ltd', 'TLS.AX', 28),
    'WES': Holding('AUD', 'ASX', 'Wesfarmers Ltd', 'WES.AX', 24),
    'MSFT': Holding('USD', 'NASDAQ', 'Microsoft Corp', 'MSFT', 21),
    'AMZN': Holding('USD', 'NASDAQ', 'Amazon.com Inc', 'AMZN', 21),
    'AAPL': Holding('USD', 'NASDAQ', 'Apple Inc', 'AAPL', 3),
}

# Income from these is a distribution (an ETF); from the rest, a dividend.
ETFS = ('VGS', 'A200', 'VAS')
# The one with AMIT cost base adjustments, one per financial year.
AMIT_INSTRUMENTS = ('VGS',)

# Monthly investing: each month a total is drawn from this range, CORE_SHARE of it goes to the core
# holdings, split by weight, while the instrument is available.
MONTHLY_RANGE = (2000, 5000)
CORE_SHARE = (0.70, 0.80)
A200_LAUNCH = dt.date(2018, 5, 7)
# (instrument, weight, first date, last date)
CORE_SCHEDULE: list[tuple[str, float, dt.date | None, dt.date | None]] = [
    ('VGS', 0.55, None, None),
    ('VAS', 0.45, None, A200_LAUNCH),
    ('A200', 0.45, A200_LAUNCH, None),
]
MONTHLY_BROKERAGE = 9.5

# Satellites: a few buys a year, 500-1,500 in the base currency each.
SATELLITES = ('MSFT', 'AMZN', 'AAPL')
SATELLITE_FIRST_YEAR = 2017
SATELLITE_BUYS_PER_YEAR = (2, 4)
SATELLITE_AMOUNT = (500, 1500)
SATELLITE_BROKERAGE = 5.0

# Single stocks bought on particular (year, month)s.
STOCK_BUYS: dict[str, list[tuple[int, int]]] = {
    'CBA': [(2017, 3), (2019, 6), (2021, 2), (2023, 5), (2025, 8)],
    'BHP': [(2018, 9), (2020, 4), (2022, 2), (2024, 1), (2025, 11)],
    'TLS': [(2016, 12), (2017, 5), (2018, 2)],
    'WES': [(2018, 11), (2019, 5), (2020, 3)],
}
STOCK_AMOUNT = (500, 1500)
STOCK_BROKERAGE = 19.95

# (instrument, date, fraction of the holding, strategy). A fraction of 1 sells it all. A MANUAL
# sale gets its SellAllocation rows written here, oldest buys first.
SELLS: list[tuple[str, dt.date, float, str]] = [
    ('VAS', dt.date(2019, 3, 14), 0.6, 'FIFO'),
    ('VAS', dt.date(2019, 8, 20), 1, 'FIFO'),
    ('TLS', dt.date(2019, 10, 15), 1, 'FIFO'),
    ('WES', dt.date(2022, 3, 22), 1, 'MANUAL'),
    ('MSFT', dt.date(2024, 11, 12), 0.3, 'MIN_CGT'),
]
# Instruments that must end up with nothing held.
FULLY_SOLD = ('VAS', 'TLS', 'WES')

CORPORATE_TAX_RATE = 30
US_WITHHOLDING_RATE = 0.15
# Share of the ETF payment kept as withholding tax: a small random fraction of it.
ETF_WITHHOLDING_RANGE = (0.0, 0.004)
ETF_JITTER = (0.97, 1.03)
# Some years' AMIT adjustment is a decrease; this is the cost base change per unit.
AMIT_PER_UNIT = (0.15, 0.85)
AMIT_DECREASE_YEARS = {2023: -0.4}


def as_date(value: Any) -> dt.date:
    """A pandas index label (a Timestamp) as a plain date."""
    return cast(dt.date, pd.Timestamp(value).date())


class FakePortfolio:
    def __init__(self, as_of: dt.date, seed: int, market: dict[str, pd.DataFrame]) -> None:
        self.as_of = as_of
        self.rng = random.Random(seed)
        self.market = market
        self.buys: list[dict[str, Any]] = []
        self.sells: list[dict[str, Any]] = []
        self.allocations: list[dict[str, Any]] = []
        self.dividends: list[dict[str, Any]] = []
        self.distributions: list[dict[str, Any]] = []
        self.adjustments: list[dict[str, Any]] = []
        self.splits: list[dict[str, Any]] = []

    # ----- market data -----

    def history(self, name: str) -> pd.DataFrame:
        return self.market[HOLDINGS[name].ticker]

    def split_dates(self, name: str) -> list[tuple[dt.date, float]]:
        """Splits from the market data since the start of the portfolio, as (ex-date, ratio)."""
        h = self.history(name)
        found = h['Stock Splits'][h['Stock Splits'] > 0]
        return [(as_date(day), float(ratio)) for day, ratio in found.items() if as_date(day) >= START]

    def later_factor(self, name: str, day: dt.date) -> float:
        """The product of the splits after `day`: Yahoo's prices are divided by it."""
        factor = 1.0
        for ex, ratio in self.split_dates(name):
            if day < ex:
                factor *= ratio
        return factor

    def price(self, name: str, day: dt.date) -> tuple[dt.date, float]:
        """As-traded close on the first trading day on or after `day`."""
        h = self.history(name)
        i = h.index.searchsorted(pd.Timestamp(day))
        trading_day = as_date(h.index[int(i)])
        return trading_day, round(float(h['Close'].iloc[i]) * self.later_factor(name, trading_day), 4)

    def usd_per_aud(self, day: dt.date) -> float:
        h = self.market['AUDUSD=X']
        return float(h['Close'].iloc[h.index.searchsorted(pd.Timestamp(day))])

    # ----- holdings -----

    def held(self, name: str, day: dt.date) -> float:
        """Units held at the close of `day`, in the units in force on `day`."""
        units = 0.0
        for buy in self.buys:
            if buy['name'] == name and buy['date'] <= day:
                units += buy['qty'] * self.later_factor(name, buy['date']) / self.later_factor(name, day)
        for sell in self.sells:
            if sell['name'] == name and sell['date'] <= day:
                units -= sell['qty']
        return units

    # ----- events -----

    def add_buy(self, name: str, day: dt.date, amount: float, brokerage: float) -> None:
        trading_day, price = self.price(name, day)
        holding = HOLDINGS[name]
        budget = amount * (self.usd_per_aud(trading_day) if holding.is_usd else 1)
        qty = max(1, math.floor((budget - brokerage) / price))
        self.buys.append(dict(name=name, date=trading_day, qty=qty, price=price,
                              brokerage=brokerage, currency=holding.currency))

    def random_dates(self, n: int, first: dt.date, last: dt.date) -> list[dt.date]:
        span = (last - first).days
        return sorted(first + dt.timedelta(days=self.rng.randint(0, span)) for _ in range(n))

    def generate_buys(self) -> None:
        month_starts = [dt.date(y, m, 1) for y in range(START.year, self.as_of.year + 1) for m in range(1, 13)
                        if START <= dt.date(y, m, 1) <= self.as_of]
        for month in month_starts:
            day = month + dt.timedelta(days=self.rng.randint(1, 5))
            if day > self.as_of - dt.timedelta(days=1):
                continue
            total = round(self.rng.uniform(*MONTHLY_RANGE) / 50) * 50
            core = total * self.rng.uniform(*CORE_SHARE)
            for name, weight, first, last in CORE_SCHEDULE:
                if (first is None or day >= first) and (last is None or day < last):
                    self.add_buy(name, day, core * weight, MONTHLY_BROKERAGE)

        for name in SATELLITES:
            splits = self.split_dates(name)
            for year in range(SATELLITE_FIRST_YEAR, self.as_of.year + 1):
                # Up to 20 December, or the month before the as-of date in the current year.
                last_month = max(self.as_of.month - 1, 1) if year == self.as_of.year else 12
                n = self.rng.randint(*SATELLITE_BUYS_PER_YEAR)
                for day in self.random_dates(n, dt.date(year, 1, 5), dt.date(year, last_month, 20)):
                    # Keep buys clear of a split's ex-date, where units and prices are in flux.
                    if any(abs((day - ex).days) <= 5 for ex, _ in splits):
                        continue
                    self.add_buy(name, day, self.rng.uniform(*SATELLITE_AMOUNT), SATELLITE_BROKERAGE)

        for name, months in STOCK_BUYS.items():
            for buy_year, buy_month in months:
                self.add_buy(name, dt.date(buy_year, buy_month, self.rng.randint(8, 20)),
                             self.rng.uniform(*STOCK_AMOUNT), STOCK_BROKERAGE)

        self.buys.sort(key=lambda b: (b['date'], b['name']))
        for i, buy in enumerate(self.buys, 1):
            buy['legacy_id'] = f'B{i:03d}'

    def generate_sells(self) -> None:
        for name, day, fraction, strategy in SELLS:
            trading_day, price = self.price(name, day)
            holding = HOLDINGS[name]
            units = self.held(name, trading_day)
            qty = int(units) if fraction >= 1 else round(units * fraction)
            self.sells.append(dict(name=name, date=trading_day, qty=qty, price=price,
                                   brokerage=SATELLITE_BROKERAGE if holding.is_usd else MONTHLY_BROKERAGE,
                                   currency=holding.currency, strategy=strategy))
        self.sells.sort(key=lambda s: (s['date'], s['name']))
        for i, sell in enumerate(self.sells, 1):
            sell['legacy_id'] = f'S{i:03d}'

        for name in FULLY_SOLD:
            last_sell = max(s['date'] for s in self.sells if s['name'] == name)
            late = [b for b in self.buys if b['name'] == name and b['date'] > last_sell]
            if late or abs(self.held(name, self.as_of)) > 1e-9:
                raise CommandError(f'{name} is meant to be fully sold, but is bought after, or held after, its last sale.')
        for name in HOLDINGS:
            if self.held(name, self.as_of) < 0:
                raise CommandError(f'{name} is sold for more than is held.')

        self.generate_allocations()

    def generate_allocations(self) -> None:
        """Rows for the MANUAL sales: each takes from the oldest buys that still have units left."""
        left: dict[str, float] = {}
        for sell in self.sells:
            if sell['strategy'] != 'MANUAL':
                continue
            name, to_allocate = sell['name'], float(sell['qty'])
            for buy in self.buys:
                if buy['name'] != name or buy['date'] >= sell['date'] or to_allocate <= 0:
                    continue
                factor = self.later_factor(name, buy['date']) / self.later_factor(name, sell['date'])
                available = left.setdefault(buy['legacy_id'], buy['qty'] * factor)
                take = min(available, to_allocate)
                if take <= 0:
                    continue
                left[buy['legacy_id']] = available - take
                to_allocate -= take
                self.allocations.append(dict(
                    legacy_id=f"{sell['legacy_id']} - {buy['legacy_id']}",
                    lookup_legacy_sell=sell['legacy_id'], lookup_legacy_buy=buy['legacy_id'], quantity=take))
            if to_allocate > 1e-9:
                raise CommandError(f"{sell['legacy_id']} sells more {name} than the buys before it provide.")

    def generate_splits(self) -> None:
        for name in HOLDINGS:
            for i, (ex, ratio) in enumerate(self.split_dates(name), 1):
                after = int(ratio) if float(ratio).is_integer() else ratio
                self.splits.append(dict(date=ex, legacy_id=f'SP-{name}-{i}', description=f'{name} 1:{after} split',
                                        instrument__name=name, quantity_before=1, quantity_after=after))
        self.splits.sort(key=lambda s: s['date'])
        for i, split in enumerate(self.splits, 1):
            split['legacy_id'] = f'SP{i}'

    def generate_income(self) -> None:
        for name, holding in HOLDINGS.items():
            h = self.history(name)
            for ex, amount in h['Dividends'][h['Dividends'] > 0].items():
                ex_date = as_date(ex)
                pay = ex_date + dt.timedelta(days=holding.pay_lag_days)
                if ex_date < START or pay > self.as_of:
                    continue
                qty = int(self.held(name, ex_date - dt.timedelta(days=1)))   # entitlement is the day before the ex-date
                if qty <= 0:
                    continue
                per = float(amount) * self.later_factor(name, ex_date)
                legacy_id = f'{pay.isoformat()}_{holding.ticker.split(".")[0]}.AX' if holding.market == 'ASX' \
                    else f'{pay.isoformat()}_{holding.ticker}'
                if name in ETFS:
                    per = round(per * self.rng.uniform(*ETF_JITTER), 6)
                    withholding = round(qty * per * self.rng.uniform(*ETF_WITHHOLDING_RANGE), 2)
                    self.distributions.append(dict(legacy_id=legacy_id, name=name, date=pay, qty=qty, per=per,
                                                   withholding=withholding, currency=holding.currency))
                    continue
                per = round(per, 6)
                row = dict(legacy_id=legacy_id, name=name, date=pay, qty=qty, currency=holding.currency)
                if holding.is_usd:
                    row.update(type='FOREIGN', unfranked=per, franked=0.0,
                               foreign_tax_credit=round(qty * per * US_WITHHOLDING_RATE, 2))
                else:
                    row.update(type='LOCAL', unfranked=0.0, franked=per, foreign_tax_credit=0.0)
                self.dividends.append(row)
        self.distributions.sort(key=lambda d: d['date'])
        self.dividends.sort(key=lambda d: d['date'])

    def generate_adjustments(self) -> None:
        for name in AMIT_INSTRUMENTS:
            for year in range(START.year + 1, self.as_of.year + 1):
                end = dt.date(year, 6, 30)
                if end > self.as_of:
                    continue
                units = self.held(name, end)
                if units <= 0:
                    continue
                amount = round(units * self.rng.uniform(*AMIT_PER_UNIT) * AMIT_DECREASE_YEARS.get(year, 1), 2)
                self.adjustments.append(dict(name=name, date=end, amount=amount))
        self.adjustments.sort(key=lambda a: (a['date'], a['name']))
        for i, adjustment in enumerate(self.adjustments, 1):
            adjustment['legacy_id'] = f'A{i:03d}'

    def generate(self) -> None:
        self.generate_buys()
        self.generate_sells()
        self.generate_splits()
        self.generate_income()
        self.generate_adjustments()

    # ----- rows as they appear in the template -----

    def table_rows(self) -> dict[str, list[dict[str, Any]]]:
        return {
            'Buy': [dict(legacy_id=b['legacy_id'], instrument__name=b['name'], date=b['date'], quantity=b['qty'],
                         unit_price=b['price'], unit_price_currency=b['currency'], total_brokerage=b['brokerage'],
                         total_brokerage_currency=b['currency']) for b in self.buys],
            'Sell': [dict(legacy_id=s['legacy_id'], instrument__name=s['name'], date=s['date'], quantity=s['qty'],
                          unit_price=s['price'], unit_price_currency=s['currency'], total_brokerage=s['brokerage'],
                          total_brokerage_currency=s['currency'], strategy=s['strategy'],
                          notes=MANUAL_NOTE if s['strategy'] == 'MANUAL' else None) for s in self.sells],
            'SellAllocation': [dict(a, notes=ALLOCATION_NOTE if i == 0 else None)
                               for i, a in enumerate(self.allocations)],
            'ShareSplit': self.splits,
            'ResidencyPeriod': [dict(legacy_id='R001', description='Australian resident throughout',
                                     status='RESIDENT', start_date=START)],
            'CostBaseAdjustment': [dict(legacy_id=a['legacy_id'], cost_base_increase_currency='AUD',
                                        cost_base_increase=a['amount'], instrument__name=a['name'],
                                        financial_year_end_date=a['date'], allocation_method='QTY_HELD')
                                   for a in self.adjustments],
            'Dividend': [dict(legacy_id=d['legacy_id'], instrument__name=d['name'], date=d['date'], quantity=d['qty'],
                              dividend_type=d['type'], unfranked_amount_per_share=d['unfranked'],
                              unfranked_amount_per_share_currency=d['currency'],
                              franked_amount_per_share=d['franked'], franked_amount_per_share_currency=d['currency'],
                              local_withholding_tax=0, local_withholding_tax_currency=d['currency'],
                              foreign_tax_credit=d['foreign_tax_credit'], foreign_tax_credit_currency=d['currency'],
                              lic_capital_gain=0, lic_capital_gain_currency=d['currency'],
                              corporate_tax_rate_percentage=CORPORATE_TAX_RATE) for d in self.dividends],
            'Distribution': [dict(legacy_id=d['legacy_id'], instrument__name=d['name'], date=d['date'],
                                  quantity=d['qty'], distribution_amount_per_share_currency=d['currency'],
                                  distribution_amount_per_share=d['per'],
                                  total_withholding_tax_currency=d['currency'], total_withholding_tax=d['withholding'])
                             for d in self.distributions],
        }


def fetch_market_data() -> dict[str, pd.DataFrame]:
    import yfinance

    symbols = [h.ticker for h in HOLDINGS.values()] + ['AUDUSD=X']
    market: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        history = yfinance.Ticker(symbol).history(start=(START - dt.timedelta(days=120)).isoformat(),
                                                  auto_adjust=False, actions=True)
        if history.empty:
            raise CommandError(f'No market data came back for {symbol}.')
        history.index = history.index.tz_localize(None).normalize()
        market[symbol] = history
    return market


def load_market_data(cache: Path | None) -> dict[str, pd.DataFrame]:
    if cache is not None and cache.exists():
        with open(cache, 'rb') as f:
            market: dict[str, pd.DataFrame] = pickle.load(f)
        return market
    market = fetch_market_data()
    if cache is not None:
        with open(cache, 'wb') as f:
            pickle.dump(market, f)
    return market


def with_fake_note(rows: list[dict[str, Any]], table_name: str) -> pd.DataFrame:
    """The table's rows as a frame, the first one marked as fake and given an example file path."""
    frame = pd.DataFrame(rows)
    frame = frame.astype(object).where(frame.notna(), None)
    if 'notes' not in frame.columns:
        frame['notes'] = None
    first_note = frame.at[0, 'notes']
    frame.at[0, 'notes'] = f'{FAKE_NOTE}. {str(first_note)}' if first_note else FAKE_NOTE
    if table_name in EXAMPLE_FILES:
        frame['file'] = None
        frame.at[0, 'file'] = EXAMPLE_FILES[table_name]
    return frame


def instrument_frame(existing: pd.DataFrame | None) -> pd.DataFrame:
    """The template's instrument list, with any instrument the portfolio uses that it lacks."""
    frame = existing.copy() if existing is not None else pd.DataFrame(columns=['name'])
    listed = set(frame['name'])
    missing = [dict(name=name, description=h.description, currency=h.currency, market__code=h.market)
               for name, h in HOLDINGS.items() if name not in listed]
    return pd.concat([frame, pd.DataFrame(missing)], ignore_index=True)


def market_frame(existing: pd.DataFrame | None) -> pd.DataFrame:
    """The template's market list, with any market the portfolio's instruments trade on that it lacks."""
    frame = existing.copy() if existing is not None else pd.DataFrame(columns=['code'])
    listed = set(frame['code'])
    missing = [dict(code=code) for code in sorted({h.market for h in HOLDINGS.values()}) if code not in listed]
    return pd.concat([frame, pd.DataFrame(missing)], ignore_index=True)


def build_fake_data(output_path: Path, as_of: dt.date, seed: int, market: dict[str, pd.DataFrame]) -> FakePortfolio:
    portfolio = FakePortfolio(as_of=as_of, seed=seed, market=market)
    portfolio.generate()

    # The market and instrument lists come from the file being replaced: the instrument list is long
    # and not something to invent. Everything else is this portfolio's, and the file is rebuilt the way
    # `make_import_template` builds one, so it has every table and its optional tables marked.
    existing = excelinterface.get_all_tables_in_excel(output_path)
    data: dict[str, pd.DataFrame] = {
        'Market': market_frame(existing.get('Market')),
        'Instrument': instrument_frame(existing.get('Instrument')),
    }
    for table_name, rows in portfolio.table_rows().items():
        if rows:
            data[table_name] = with_fake_note(rows, table_name)
    make_import_template.build_template(output_path, data)

    workbook = openpyxl.load_workbook(output_path)
    workbook['Index']['G1'] = FAKE_NOTE
    workbook.save(output_path)
    return portfolio


class Command(BaseCommand):
    help = ('Fill an import template with a fake 10-year portfolio, for the sample data. '
            'Overwrites the tables it writes, so --force is needed.')

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            '--output',
            default=None,
            help='The template to fill in, written in place. Defaults to '
                 'share_dinkum_app/import_data/data_import_template_public.xlsx',
        )
        parser.add_argument('--force', action='store_true', help='Overwrite the file.')
        parser.add_argument('--seed', type=int, default=DEFAULT_SEED,
                            help=f'Random seed. The same seed and market data give the same file. Default {DEFAULT_SEED}.')
        parser.add_argument('--as-of', default=None, help='Treat this date (YYYY-MM-DD) as today. Defaults to today.')
        parser.add_argument('--cache', default=None,
                            help='A file to keep the downloaded market data in. If it exists it is used instead of '
                                 'downloading, which also keeps the output from drifting as Yahoo adds new data.')

    def handle(self, *args: Any, **options: Any) -> None:
        default_path = Path(__file__).resolve().parents[2] / 'import_data' / 'data_import_template_public.xlsx'
        output_path = Path(options['output']).resolve() if options['output'] else default_path
        if not output_path.exists():
            raise CommandError(f'{output_path} does not exist. Fill in an existing template: it supplies the sheets.')
        if not options['force']:
            raise CommandError(f'This replaces the data in {output_path}. Pass --force to go ahead.')

        as_of = dt.date.fromisoformat(options['as_of']) if options['as_of'] else dt.date.today()
        cache = Path(options['cache']).resolve() if options['cache'] else None
        portfolio = build_fake_data(output_path, as_of, options['seed'], load_market_data(cache))

        self.stdout.write(self.style.SUCCESS(f'Wrote a fake portfolio to {output_path}'))
        self.stdout.write(
            f'{len(portfolio.buys)} buys, {len(portfolio.sells)} sells ({len(portfolio.allocations)} manual allocations), '
            f'{len(portfolio.splits)} splits, {len(portfolio.adjustments)} cost base adjustments, '
            f'{len(portfolio.dividends)} dividends, {len(portfolio.distributions)} distributions.')
