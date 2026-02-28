"""
Comprehensive test suite for share_dinkum_app.

Run with: python manage.py test share_dinkum_app
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch, MagicMock

import pandas as pd

from django.test import TestCase, TransactionTestCase
from django.db import IntegrityError
from djmoney.money import Money

from share_dinkum_app.constants import DEFAULT_CURRENCY, CGT_DISCOUNT_RATE, CGT_DISCOUNT_THRESHOLD_DAYS
from share_dinkum_app.models import (
    AppUser,
    FiscalYearType,
    FiscalYear,
    Account,
    Market,
    Instrument,
    ExchangeRate,
    CurrentExchangeRate,
    Buy,
    Sell,
    Parcel,
    SellAllocation,
    ShareSplit,
    CostBaseAdjustment,
    CostBaseAdjustmentAllocation,
    LogEntry,
    InstrumentPriceHistory,
    Dividend,
    DataExport,
)
from share_dinkum_app.utils.currency import add_currencies
from share_dinkum_app.utils.filefield_operations import user_directory_path, process_filefield
from share_dinkum_app.decorators import safe_property
from share_dinkum_app.reports import RealisedCapitalGainReport
from share_dinkum_app import yfinanceinterface


# --- Test data factories (minimal objects for isolation) ---


def create_fiscal_year_type(description='Australian Tax Year', start_month=7, start_day=1):
    return FiscalYearType.objects.create(
        description=description,
        start_month=start_month,
        start_day=start_day,
    )


def create_user(username='testuser', password='testpass123'):
    return AppUser.objects.create_user(username=username, password=password)


def create_account(owner=None, currency=DEFAULT_CURRENCY, description='Test Account'):
    if owner is None:
        owner = create_user()
    fy_type = create_fiscal_year_type()
    return Account.objects.create(
        owner=owner,
        description=description,
        currency=currency,
        fiscal_year_type=fy_type,
    )


def create_market(account=None, code='ASX', suffix='AX'):
    if account is None:
        account = create_account()
    return Market.objects.create(account=account, code=code, suffix=suffix)


def create_instrument(account=None, market=None, name='BHP', currency=DEFAULT_CURRENCY):
    if account is None:
        account = create_account()
    if market is None:
        market = create_market(account=account)
    return Instrument.objects.create(
        account=account,
        market=market,
        name=name,
        description=f'{name} description',
        currency=currency,
    )


def create_exchange_rate(account, convert_from, convert_to, rate=Decimal('1.5'), exchange_date=None):
    if exchange_date is None:
        exchange_date = date.today()
    return ExchangeRate.objects.create(
        account=account,
        convert_from=convert_from,
        convert_to=convert_to,
        date=exchange_date,
        exchange_rate_multiplier=rate,
    )


# =============================================================================
# Utils: currency
# =============================================================================


class AddCurrenciesTests(TestCase):
    """Tests for share_dinkum_app.utils.currency.add_currencies."""

    def test_empty_returns_zero_in_default_currency(self):
        result = add_currencies()
        self.assertEqual(result.amount, 0)
        self.assertEqual(str(result.currency), DEFAULT_CURRENCY)

    def test_single_zero_returns_zero(self):
        result = add_currencies(Money(0, 'AUD'))
        self.assertEqual(result.amount, 0)
        self.assertEqual(str(result.currency), DEFAULT_CURRENCY)

    def test_multiple_zeros_returns_zero(self):
        result = add_currencies(Money(0, 'AUD'), Money(0, 'USD'))
        self.assertEqual(result.amount, 0)

    def test_single_nonzero_returns_same(self):
        result = add_currencies(Money(100, 'AUD'))
        self.assertEqual(result.amount, 100)
        self.assertEqual(str(result.currency), 'AUD')

    def test_same_currency_sums(self):
        result = add_currencies(Money(10, 'AUD'), Money(20, 'AUD'), Money(5, 'AUD'))
        self.assertEqual(result.amount, 35)
        self.assertEqual(str(result.currency), 'AUD')

    def test_ignores_zero_amounts_in_mix(self):
        result = add_currencies(Money(0, 'AUD'), Money(10, 'AUD'), Money(0, 'AUD'))
        self.assertEqual(result.amount, 10)

    def test_different_currencies_raises(self):
        with self.assertRaises(ValueError) as ctx:
            add_currencies(Money(10, 'AUD'), Money(20, 'USD'))
        self.assertIn('Cannot add different currencies', str(ctx.exception))

    def test_non_money_raises(self):
        with self.assertRaises(TypeError) as ctx:
            add_currencies(10, Money(20, 'AUD'))
        self.assertIn('Expected Money', str(ctx.exception))


# =============================================================================
# Utils: filefield_operations
# =============================================================================


class UserDirectoryPathTests(TestCase):
    """Tests for user_directory_path upload_to helper."""

    def test_with_account_and_date(self):
        acc = create_account()
        obj = type('Obj', (), {})()
        obj.account = acc
        obj.date = date(2024, 6, 15)
        path = user_directory_path(obj, 'statement.pdf')
        self.assertIn(str(acc.id), path)
        self.assertIn('2024-06-15', path)
        self.assertIn('statement.pdf', path)

    def test_with_instrument(self):
        acc = create_account()
        obj = type('Obj', (), {})()
        obj.account = acc
        obj.instrument = type('Inst', (), {'name': 'BHP'})()
        obj.date = date(2024, 1, 1)
        path = user_directory_path(obj, 'file.xlsx')
        self.assertIn('BHP', path)


class ProcessFilefieldTests(TestCase):
    """Tests for process_filefield (minimal: no files on disk)."""

    def test_none_returns_none(self):
        self.assertIsNone(process_filefield(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(process_filefield(''))


# =============================================================================
# yfinanceinterface
# =============================================================================


class ToSnakeCaseTests(TestCase):
    """Tests for yfinanceinterface.to_snake_case."""

    def test_lowercase_unchanged(self):
        self.assertEqual(yfinanceinterface.to_snake_case('open'), 'open')

    def test_camel_case_converted(self):
        # to_snake_case only replaces non-alphanumeric and lowercases; it does not split CamelCase
        self.assertEqual(yfinanceinterface.to_snake_case('StockSplits'), 'stocksplits')

    def test_mixed_case_with_space_becomes_underscore(self):
        self.assertEqual(yfinanceinterface.to_snake_case('Stock Splits'), 'stock_splits')

    def test_non_alnum_replaced_with_underscore(self):
        self.assertEqual(yfinanceinterface.to_snake_case('Close'), 'close')
        self.assertEqual(yfinanceinterface.to_snake_case('High'), 'high')


@patch('share_dinkum_app.yfinanceinterface.yf')
class GetExchangeRateTests(TestCase):
    """Tests for yfinanceinterface.get_exchange_rate."""

    def test_returns_decimal_when_history_has_data(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.return_value = pd.DataFrame({'Close': [1.55]})
        result = yfinanceinterface.get_exchange_rate('USD', 'AUD', exchange_date=date(2024, 1, 15))
        # Decimal(float) can have float noise; compare quantized or as float
        self.assertEqual(result.quantize(Decimal('0.01')), Decimal('1.55'))
        mock_ticker.history.assert_called_once()

    def test_returns_none_on_exception(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.side_effect = Exception('network error')
        result = yfinanceinterface.get_exchange_rate('USD', 'AUD', exchange_date=date(2024, 1, 15))
        self.assertIsNone(result)

    def test_returns_none_when_history_empty(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.return_value = pd.DataFrame({'Close': []})
        result = yfinanceinterface.get_exchange_rate('USD', 'AUD', exchange_date=date(2024, 1, 15))
        self.assertIsNone(result)

    def test_uses_today_when_exchange_date_none(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.return_value = pd.DataFrame({'Close': [2.0]})
        result = yfinanceinterface.get_exchange_rate('USD', 'AUD', exchange_date=None)
        self.assertEqual(result.quantize(Decimal('0.01')), Decimal('2.00'))


@patch('share_dinkum_app.yfinanceinterface.yf')
class GetExchangeRateHistoryTests(TestCase):
    """Tests for yfinanceinterface.get_exchange_rate_history."""

    def test_returns_dataframe_with_expected_columns_on_success(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        # history() returns DataFrame with Date index; code resets index and renames cols to snake_case
        df = pd.DataFrame({
            'Open': [1.0], 'High': [1.1], 'Low': [0.9], 'Close': [1.05],
            'Volume': [1000], 'Stock Splits': [0],
        }, index=pd.DatetimeIndex([pd.Timestamp('2024-01-15')]))
        df.index.name = 'Date'
        mock_ticker.history.return_value = df.copy()
        result = yfinanceinterface.get_exchange_rate_history('USD', 'AUD', start_date=date(2024, 1, 1))
        self.assertFalse(result.empty)
        self.assertIn('convert_from', result.columns)
        self.assertIn('convert_to', result.columns)
        self.assertIn('date', result.columns)
        self.assertIn('exchange_rate_multiplier', result.columns)
        self.assertEqual(result['convert_from'].iloc[0], 'USD')
        self.assertEqual(result['convert_to'].iloc[0], 'AUD')

    def test_returns_empty_dataframe_on_exception(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.side_effect = Exception('api error')
        result = yfinanceinterface.get_exchange_rate_history('USD', 'AUD', start_date=date(2024, 1, 1))
        self.assertTrue(result.empty)
        self.assertIsInstance(result, pd.DataFrame)


@patch('share_dinkum_app.yfinanceinterface.yf')
class GetInstrumentPriceHistoryTests(TestCase):
    """Tests for yfinanceinterface.get_instrument_price_history."""

    def test_returns_dataframe_with_expected_columns_on_success(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        df = pd.DataFrame({
            'Open': [50.0], 'High': [51.0], 'Low': [49.0], 'Close': [50.5],
            'Volume': [1000000], 'Stock Splits': [0],
        }, index=pd.DatetimeIndex([pd.Timestamp('2024-01-15')]))
        df.index.name = 'Date'
        mock_ticker.history.return_value = df.copy()
        result = yfinanceinterface.get_instrument_price_history(instrument, start_date=date(2024, 1, 1))
        self.assertFalse(result.empty)
        self.assertIn('instrument', result.columns)
        self.assertIn('date', result.columns)
        self.assertIn('close', result.columns)
        self.assertIn('open', result.columns)
        self.assertIn('volume', result.columns)
        self.assertIn('stock_splits', result.columns)

    def test_returns_empty_dataframe_on_exception(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.side_effect = Exception('api error')
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_instrument_price_history(instrument, start_date=date(2024, 1, 1))
        self.assertTrue(result.empty)
        self.assertIsInstance(result, pd.DataFrame)


# =============================================================================
# Decorators
# =============================================================================


class SafePropertyTests(TestCase):
    """Tests for @safe_property decorator."""

    def test_returns_none_when_adding(self):
        class Model:
            _state = type('State', (), {'adding': True})()
            def get_val(self):
                return 42
        Model.get_val = safe_property(Model.get_val)
        m = Model()
        self.assertIsNone(m.get_val)

    def test_returns_value_when_not_adding(self):
        class Model:
            _state = type('State', (), {'adding': False})()
            def get_val(self):
                return 42
        Model.get_val = safe_property(Model.get_val)
        m = Model()
        self.assertEqual(m.get_val, 42)

    def test_fget_has_safe_property_marker(self):
        def fn(self):
            return 1
        wrapped = safe_property(fn)
        self.assertTrue(getattr(wrapped.fget, '_is_safe_property', False))


# =============================================================================
# Models: FiscalYearType & FiscalYear
# =============================================================================


class FiscalYearTypeTests(TestCase):
    """Tests for FiscalYearType model."""

    def test_classify_date_same_calendar_year(self):
        fy_type = create_fiscal_year_type(start_month=7, start_day=1)
        fy, created = fy_type.classify_date(date(2024, 8, 1))
        self.assertTrue(created)
        self.assertEqual(fy.start_year, 2024)
        self.assertEqual(fy.fiscal_year_type, fy_type)

    def test_classify_date_previous_calendar_year(self):
        fy_type = create_fiscal_year_type(start_month=7, start_day=1)
        fy, created = fy_type.classify_date(date(2024, 3, 1))
        self.assertTrue(created)
        self.assertEqual(fy.start_year, 2023)

    def test_classify_date_get_or_create_reuse(self):
        fy_type = create_fiscal_year_type()
        fy1, c1 = fy_type.classify_date(date(2024, 8, 1))
        fy2, c2 = fy_type.classify_date(date(2024, 9, 1))
        self.assertFalse(c2)
        self.assertEqual(fy1.id, fy2.id)

    def test_str(self):
        fy_type = create_fiscal_year_type(description='AU Tax')
        self.assertIn('AU Tax', str(fy_type))


class FiscalYearTests(TestCase):
    """Tests for FiscalYear model."""

    def test_start_date_end_date_july_year(self):
        fy_type = create_fiscal_year_type(start_month=7, start_day=1)
        fy = FiscalYear.objects.create(fiscal_year_type=fy_type, start_year=2024)
        self.assertEqual(fy.start_date, date(2024, 7, 1))
        self.assertEqual(fy.end_date, date(2025, 6, 30))

    def test_get_name_financial_year(self):
        fy_type = create_fiscal_year_type(start_month=7, start_day=1)
        fy = FiscalYear.objects.create(fiscal_year_type=fy_type, start_year=2024)
        self.assertEqual(fy.get_name(), 'FY2024/25')

    def test_get_name_calendar_year(self):
        fy_type = FiscalYearType.objects.create(
            description='Calendar',
            start_month=1,
            start_day=1,
        )
        fy = FiscalYear.objects.create(fiscal_year_type=fy_type, start_year=2024)
        self.assertEqual(fy.get_name(), '2024')


# =============================================================================
# Models: AppUser, Account
# =============================================================================


class AppUserTests(TestCase):
    """Tests for AppUser model."""

    def test_save_sets_blank_first_last_name(self):
        user = AppUser(username='u', email='u@test.com')
        user.set_password('x')
        user.save()
        self.assertEqual(user.first_name, '')
        self.assertEqual(user.last_name, '')


class AccountTests(TestCase):
    """Tests for Account model."""

    def test_str(self):
        acc = create_account()
        self.assertIn(acc.description, str(acc))
        self.assertIn(acc.currency, str(acc))

    def test_portfolio_value_converted_empty_is_zero(self):
        acc = create_account()
        self.assertEqual(acc.portfolio_value_converted.amount, 0)
        self.assertEqual(str(acc.portfolio_value_converted.currency), acc.currency)


# =============================================================================
# Models: Exchange rates (with mocked yfinance)
# =============================================================================


@patch('share_dinkum_app.models.yfinanceinterface.get_exchange_rate')
class ExchangeRateTests(TestCase):
    """Tests for ExchangeRate and AbstractExchangeRate (with yfinance mocked)."""

    def test_apply_same_currency(self, mock_get_rate):
        acc = create_account()
        rate = create_exchange_rate(acc, 'AUD', 'AUD', rate=Decimal('1'))
        m = Money(100, 'AUD')
        result = rate.apply(m)
        self.assertEqual(result.amount, 100)
        self.assertEqual(str(result.currency), 'AUD')

    def test_apply_converts(self, mock_get_rate):
        acc = create_account()
        rate = create_exchange_rate(acc, 'USD', 'AUD', rate=Decimal('1.5'))
        m = Money(100, 'USD')
        result = rate.apply(m)
        self.assertEqual(result.amount, Decimal('150'))
        self.assertEqual(str(result.currency), 'AUD')

    def test_apply_wrong_currency_raises(self, mock_get_rate):
        acc = create_account()
        rate = create_exchange_rate(acc, 'USD', 'AUD', rate=Decimal('1.5'))
        m = Money(100, 'AUD')
        with self.assertRaises(AssertionError):
            rate.apply(m)

    def test_update_current_creates_current_rate(self, mock_get_rate):
        acc = create_account()
        hist = create_exchange_rate(acc, 'USD', 'AUD', rate=Decimal('1.6'))
        current = hist.update_current()
        self.assertIsNotNone(current)
        self.assertEqual(current.exchange_rate_multiplier, Decimal('1.6'))
        self.assertEqual(CurrentExchangeRate.objects.filter(
            account=acc, convert_from='USD', convert_to='AUD'
        ).count(), 1)

    def test_get_or_create_creates_with_mock_rate(self, mock_get_rate):
        mock_get_rate.return_value = Decimal('1.55')
        acc = create_account()
        rate = ExchangeRate.get_or_create(
            account=acc,
            convert_from='USD',
            convert_to='AUD',
            exchange_date=date(2024, 1, 15),
        )
        self.assertEqual(rate.exchange_rate_multiplier, Decimal('1.55'))
        mock_get_rate.assert_called_once()


# =============================================================================
# Models: Market, Instrument
# =============================================================================


class MarketTests(TestCase):
    """Tests for Market model."""

    def test_unique_per_account_code(self):
        acc = create_account()
        Market.objects.create(account=acc, code='ASX')
        with self.assertRaises(IntegrityError):
            Market.objects.create(account=acc, code='ASX')


class InstrumentTests(TestCase):
    """Tests for Instrument model."""

    def test_yfinance_ticker_code_with_suffix(self):
        acc = create_account()
        market = Market.objects.create(account=acc, code='ASX', suffix='.AX')
        inst = Instrument.objects.create(
            account=acc, market=market, name='BHP',
            description='BHP', currency='AUD',
        )
        self.assertEqual(inst.yfinance_ticker_code, 'BHP.AX')

    def test_yfinance_ticker_code_no_suffix(self):
        acc = create_account()
        market = Market.objects.create(account=acc, code='NASDAQ', suffix='')
        inst = Instrument.objects.create(
            account=acc, market=market, name='AAPL',
            description='Apple', currency='USD',
        )
        self.assertEqual(inst.yfinance_ticker_code, 'AAPL')

    def test_quantity_held_no_trades_is_zero(self):
        inst = create_instrument()
        self.assertEqual(inst.quantity_held, 0)

    def test_value_held_no_price_is_zero(self):
        inst = create_instrument()
        self.assertEqual(inst.value_held.amount, 0)


# =============================================================================
# Models: Buy, Sell, Parcel, SellAllocation (with signals)
# =============================================================================


class BuyAndParcelSignalsTests(TransactionTestCase):
    """Test Buy creation creates Parcel via signal."""

    def test_create_buy_creates_parcel(self):
        acc = create_account()
        market = create_market(account=acc)
        inst = create_instrument(account=acc, market=market)
        buy = Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 10),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(10, 'AUD'),
        )
        parcels = Parcel.objects.filter(buy=buy)
        self.assertEqual(parcels.count(), 1)
        self.assertEqual(parcels.first().parcel_quantity, Decimal('100'))


class SellAllocationTests(TransactionTestCase):
    """Test Sell with strategy creates allocations and parcel bifurcation."""

    def test_sell_fifo_creates_allocations(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 5),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 10),
            quantity=Decimal('50'),
            unit_price=Money(52, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        sell = Sell.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 2, 1),
            quantity=Decimal('75'),
            unit_price=Money(55, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
            strategy='FIFO',
        )
        allocations = SellAllocation.objects.filter(sell=sell, is_active=True)
        self.assertGreaterEqual(allocations.count(), 1)
        total_allocated = sum(a.quantity for a in allocations)
        self.assertEqual(total_allocated, Decimal('75'))

    def test_sell_manual_no_auto_allocations(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 5),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        sell = Sell.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 2, 1),
            quantity=Decimal('50'),
            unit_price=Money(55, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
            strategy='MANUAL',
        )
        allocations = SellAllocation.objects.filter(sell=sell)
        self.assertEqual(allocations.count(), 0)


class ParcelTests(TransactionTestCase):
    """Tests for Parcel model methods (bifurcate, split, cost base)."""

    def test_remaining_quantity_after_buy(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 5),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        parcel = Parcel.objects.get(buy__instrument=inst)
        self.assertEqual(parcel.remaining_quantity, Decimal('100'))

    def test_split_or_consolidate_multiplier(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 5),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        parcel = Parcel.objects.get(buy__instrument=inst)
        new_parcel = parcel.split_or_consolidate(multiplier=Decimal('2'), date=date(2024, 2, 1))
        self.assertEqual(new_parcel.parcel_quantity, Decimal('200'))
        self.assertEqual(new_parcel.cumulative_split_multiplier, Decimal('2'))
        parcel.refresh_from_db()
        self.assertIsNotNone(parcel.deactivation_date)


# =============================================================================
# Models: ShareSplit
# =============================================================================


class ShareSplitTests(TransactionTestCase):
    """Tests for ShareSplit model."""

    def test_split_multiplier(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        ss = ShareSplit.objects.create(
            account=acc,
            instrument=inst,
            quantity_before=Decimal('1'),
            quantity_after=Decimal('3'),
            date=date(2024, 3, 1),
        )
        self.assertEqual(ss.split_multiplier, Decimal('3'))


# =============================================================================
# Models: CostBaseAdjustment
# =============================================================================


class CostBaseAdjustmentTests(TestCase):
    """Tests for CostBaseAdjustment model."""

    def test_get_description(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        adj = CostBaseAdjustment(
            account=acc,
            instrument=inst,
            financial_year_end_date=date(2024, 6, 30),
            cost_base_increase=Money(100, 'AUD'),
            allocation_method='MANUAL',
        )
        adj.save()
        desc = adj.get_description()
        self.assertIn('2024-06-30', desc)
        self.assertIn(inst.name, desc)
        self.assertIn('100', desc)


# =============================================================================
# Models: LogEntry, BaseModel
# =============================================================================


class LogEntryTests(TestCase):
    """Tests for LogEntry and BaseModel.log_event."""

    def test_log_event_creates_entry(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        inst.log_event('Test event')
        entries = LogEntry.objects.filter(account=acc, object_id=inst.id)
        self.assertEqual(entries.count(), 1)
        self.assertIn('Test event', entries.first().event)

    def test_log_entry_str_format(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        inst.log_event('Something happened')
        entry = LogEntry.objects.filter(account=acc).first()
        self.assertIn('Something happened', str(entry))
        self.assertIn(str(entry.pk)[-4:], str(entry))


# =============================================================================
# Models: InstrumentPriceHistory
# =============================================================================


class InstrumentPriceHistoryTests(TestCase):
    """Tests for InstrumentPriceHistory (get_absolute_url)."""

    def test_get_absolute_url(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        iph = InstrumentPriceHistory.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 1, 15),
            open=Decimal('10'),
            high=Decimal('11'),
            low=Decimal('9'),
            close=Decimal('10.5'),
            volume=1000,
            stock_splits=Decimal('0'),
        )
        url = iph.get_absolute_url()
        self.assertIn('admin', url)
        self.assertIn(str(iph.id), url)


# =============================================================================
# Models: Dividend
# =============================================================================


class DividendTests(TestCase):
    """Tests for Dividend model calculated fields."""

    def test_total_franked_amount(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        div = Dividend.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 4, 1),
            quantity=Decimal('100'),
            franked_amount_per_share=Money(Decimal('0.50'), 'AUD'),
            unfranked_amount_per_share=Money(0, 'AUD'),
        )
        self.assertEqual(div.total_franked_amount.amount, Decimal('50'))
        self.assertEqual(str(div.total_franked_amount.currency), 'AUD')


# =============================================================================
# Reports
# =============================================================================


class RealisedCapitalGainReportTests(TestCase):
    """Tests for RealisedCapitalGainReport."""

    def test_generate_empty_account(self):
        acc = create_account()
        report = RealisedCapitalGainReport(account=acc)
        df = report.generate()
        self.assertTrue(df.empty)
        self.assertEqual(
            list(df.columns),
            [
                'sell_date', 'instrument', 'quantity_sold', 'buy_id', 'parcel_id', 'sell_id',
                'sell_allocation_id', 'buy_date', 'days_held', 'proceeds', 'cost_base',
                'capital_gain', 'fiscal_year',
            ],
        )

    def test_generate_with_one_sale_allocation(self):
        acc = create_account()
        inst = create_instrument(account=acc)
        buy = Buy.objects.create(
            account=acc,
            instrument=inst,
            date=date(2023, 1, 5),
            quantity=Decimal('100'),
            unit_price=Money(50, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        sell = Sell.objects.create(
            account=acc,
            instrument=inst,
            date=date(2024, 2, 1),
            quantity=Decimal('50'),
            unit_price=Money(60, 'AUD'),
            total_brokerage=Money(0, 'AUD'),
            strategy='FIFO',
        )
        report = RealisedCapitalGainReport(account=acc)
        df = report.generate()
        self.assertEqual(len(df), 1)
        self.assertEqual(df.iloc[0]['instrument'], inst.name)
        self.assertEqual(df.iloc[0]['quantity_sold'], Decimal('50'))
        self.assertIn('capital_gain', df.columns)


# =============================================================================
# Signals: default account
# =============================================================================


class AssignDefaultAccountSignalTests(TransactionTestCase):
    """Test that first account becomes user's default_account."""

    def test_first_account_set_as_default(self):
        user = create_user()
        self.assertIsNone(user.default_account_id)
        acc = create_account(owner=user)
        user.refresh_from_db()
        self.assertEqual(user.default_account_id, acc.id)


# =============================================================================
# Constants
# =============================================================================


class ConstantsTests(TestCase):
    """Sanity check for constants used in logic."""

    def test_cgt_constants(self):
        self.assertEqual(CGT_DISCOUNT_RATE, 0.5)
        self.assertEqual(CGT_DISCOUNT_THRESHOLD_DAYS, 365)

    def test_default_currency(self):
        self.assertEqual(DEFAULT_CURRENCY, 'AUD')
