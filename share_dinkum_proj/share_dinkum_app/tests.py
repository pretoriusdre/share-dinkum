"""
Comprehensive test suite for share_dinkum_app.

Run with: python manage.py test share_dinkum_app
"""
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

from django.core.management import call_command
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
from share_dinkum_app import excelinterface, loading, version, yfinanceinterface
from share_dinkum_app.management.commands import make_import_template


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
        mock_ticker.history.assert_called_once_with(start='2024-01-01')


    def test_returns_empty_dataframe_on_exception(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.history.side_effect = Exception('api error')
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_instrument_price_history(instrument, start_date=date(2024, 1, 1))
        self.assertTrue(result.empty)
        self.assertIsInstance(result, pd.DataFrame)
        mock_ticker.history.assert_called_once_with(start='2024-01-01')



    def test_end_date_is_inclusive_and_forwarded_to_history(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        df = pd.DataFrame({
            'Open': [50.0], 'High': [51.0], 'Low': [49.0], 'Close': [50.5],
            'Volume': [1000000], 'Stock Splits': [0],
        }, index=pd.DatetimeIndex([pd.Timestamp('2024-01-31')]))
        df.index.name = 'Date'
        mock_ticker.history.return_value = df.copy()
        result = yfinanceinterface.get_instrument_price_history(
            instrument,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
        )
        self.assertFalse(result.empty)
        mock_ticker.history.assert_called_once_with(start='2024-01-01', end='2024-02-01')


@patch('share_dinkum_app.yfinanceinterface.yf')
class GetCurrentPriceTests(TestCase):
    """Tests for yfinanceinterface.get_current_price."""

    def test_returns_decimal_when_current_price_exists(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.info = {'currentPrice': 150.25}
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_current_price(instrument)
        self.assertEqual(result.quantize(Decimal('0.01')), Decimal('150.25'))

    def test_falls_back_to_regular_market_price(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.info = {'regularMarketPrice': 148.50}
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_current_price(instrument)
        self.assertEqual(result.quantize(Decimal('0.01')), Decimal('148.50'))

    def test_prefers_current_price_over_regular_market_price(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.info = {'currentPrice': 150.25, 'regularMarketPrice': 148.50}
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_current_price(instrument)
        self.assertEqual(result.quantize(Decimal('0.01')), Decimal('150.25'))

    def test_returns_none_when_no_price_available(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.info = {}
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_current_price(instrument)
        self.assertIsNone(result)

    def test_returns_none_on_exception(self, mock_yf):
        mock_ticker = MagicMock()
        mock_yf.Ticker.return_value = mock_ticker
        mock_ticker.info = property(lambda self: (_ for _ in ()).throw(Exception('network error')))
        type(mock_ticker).info = property(lambda self: (_ for _ in ()).throw(Exception('network error')))
        instrument = MagicMock()
        instrument.yfinance_ticker_code = 'BHP.AX'
        result = yfinanceinterface.get_current_price(instrument)
        self.assertIsNone(result)


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

    def test_classify_date_boundaries_default_parameters(self):
        fy_type = FiscalYearType.objects.create(
            description='Australian Tax Year',
            start_month=7,
            start_day=1,
        )

        scenarios = [
            (date(2024, 6, 30), 2023),
            (date(2024, 7, 1), 2024),
            (date(2024, 12, 31), 2024),
            (date(2025, 1, 1), 2024),
        ]

        encountered_years = {}

        for input_date, expected_year in scenarios:
            fiscal_year, _ = fy_type.classify_date(input_date)
            self.assertEqual(
                fiscal_year.start_year,
                expected_year,
                f"{input_date} should map to fiscal year starting {expected_year}",
            )
            if expected_year in encountered_years:
                self.assertEqual(encountered_years[expected_year], fiscal_year.id)
            else:
                encountered_years[expected_year] = fiscal_year.id

    def test_classify_date_calendar_year_boundaries(self):
        fy_type = FiscalYearType.objects.create(
            description='Calendar Year',
            start_month=1,
            start_day=1,
        )

        scenarios = [
            (date(2023, 12, 31), 2023),
            (date(2024, 1, 1), 2024),
            (date(2024, 2, 29), 2024),
            (date(2024, 12, 31), 2024),
            (date(2025, 1, 1), 2025),
        ]

        encountered_years = {}

        for input_date, expected_year in scenarios:
            fiscal_year, _ = fy_type.classify_date(input_date)
            self.assertEqual(
                fiscal_year.start_year,
                expected_year,
                f"{input_date} should map to fiscal year starting {expected_year}",
            )
            if expected_year in encountered_years:
                self.assertEqual(encountered_years[expected_year], fiscal_year.id)
            else:
                encountered_years[expected_year] = fiscal_year.id

        fy_mid_year, _ = fy_type.classify_date(date(2024, 6, 30))
        fy_late_year, _ = fy_type.classify_date(date(2024, 11, 15))
        self.assertEqual(fy_mid_year.id, fy_late_year.id)



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

    def test_calendar_year_start_and_end_dates_cover_full_year(self):
        fy_type = FiscalYearType.objects.create(
            description='Calendar Year',
            start_month=1,
            start_day=1,
        )
        fy = FiscalYear.objects.create(fiscal_year_type=fy_type, start_year=2024)

        self.assertEqual(fy.start_date, date(2024, 1, 1))
        self.assertEqual(fy.end_date, date(2024, 12, 31))
        self.assertEqual(
            (fy.end_date - fy.start_date).days + 1,
            366,
            "Leap year should span all 366 days",
        )

        fy_non_leap = FiscalYear.objects.create(fiscal_year_type=fy_type, start_year=2023)
        self.assertEqual(fy_non_leap.start_date, date(2023, 1, 1))
        self.assertEqual(fy_non_leap.end_date, date(2023, 12, 31))
        self.assertEqual(
            (fy_non_leap.end_date - fy_non_leap.start_date).days + 1,
            365,
            "Non-leap fiscal year should span 365 days",
        )




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


# =============================================================================
# Loading: importing files into more than one portfolio
# =============================================================================


class ImportWorkbookMixin:
    """Builds the kind of file a person fills in, so the tests go through the real loading path."""

    def build_workbook(self, path, markets=None, instruments=None, buys=None, market_ids=None):
        if markets is None:
            markets = [{'code': 'ASX', 'description': 'Australian Securities Exchange', 'suffix': 'AX'}]
        if instruments is None:
            instruments = [{'name': 'BHP', 'description': 'BHP Group', 'currency': 'AUD', 'market__code': 'ASX'}]
        if buys is None:
            buys = [{
                'legacy_id': 'buy-1',
                'instrument__name': 'BHP',
                'date': date(2023, 7, 1),
                'quantity': Decimal('100'),
                'unit_price': Decimal('40'),
                'unit_price_currency': 'AUD',
                'total_brokerage': Decimal('10'),
                'total_brokerage_currency': 'AUD',
            }]

        if market_ids is not None:
            markets = [dict(market, id=market_id) for market, market_id in zip(markets, market_ids)]

        generator = excelinterface.ExcelGen(title='Test import')
        generator.add_table(pd.DataFrame(markets), table_name='Market')
        generator.add_table(pd.DataFrame(instruments), table_name='Instrument')
        generator.add_table(pd.DataFrame(buys), table_name='Buy')
        generator.save(path)
        return path


class DataLoaderMultiPortfolioTests(ImportWorkbookMixin, TransactionTestCase):
    """A file is loaded into exactly one portfolio, and never at the expense of another."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.workbook = Path(self.temp_dir.name) / 'import.xlsx'

        self.owner = create_user(username='owner')
        fy_type = create_fiscal_year_type()
        self.account_a = Account.objects.create(
            owner=self.owner, description='Portfolio A', currency='AUD', fiscal_year_type=fy_type)
        self.account_b = Account.objects.create(
            owner=self.owner, description='Portfolio B', currency='AUD', fiscal_year_type=fy_type)

    def test_each_portfolio_gets_its_own_records(self):
        self.build_workbook(self.workbook)

        loading.DataLoader(account=self.account_a, input_file=self.workbook)
        loading.DataLoader(account=self.account_b, input_file=self.workbook)

        for account in (self.account_a, self.account_b):
            self.assertEqual(Market.objects.filter(account=account).count(), 1)
            self.assertEqual(Instrument.objects.filter(account=account).count(), 1)
            self.assertEqual(Buy.objects.filter(account=account).count(), 1)
            self.assertEqual(Parcel.objects.filter(account=account).count(), 1)

    def test_loading_a_second_portfolio_leaves_the_first_alone(self):
        self.build_workbook(self.workbook)
        loading.DataLoader(account=self.account_a, input_file=self.workbook)

        buy_ids_before = set(Buy.objects.filter(account=self.account_a).values_list('id', flat=True))

        other_workbook = Path(self.temp_dir.name) / 'other.xlsx'
        self.build_workbook(
            other_workbook,
            instruments=[{'name': 'CBA', 'description': 'CBA', 'currency': 'AUD', 'market__code': 'ASX'}],
            buys=[{
                'legacy_id': 'buy-99',
                'instrument__name': 'CBA',
                'date': date(2024, 2, 1),
                'quantity': Decimal('5'),
                'unit_price': Decimal('100'),
                'unit_price_currency': 'AUD',
                'total_brokerage': Decimal('10'),
                'total_brokerage_currency': 'AUD',
            }],
        )
        loading.DataLoader(account=self.account_b, input_file=other_workbook)

        buy_ids_after = set(Buy.objects.filter(account=self.account_a).values_list('id', flat=True))
        self.assertEqual(buy_ids_before, buy_ids_after)
        self.assertEqual(Instrument.objects.filter(account=self.account_a, name='CBA').count(), 0)

    def test_loading_the_same_file_twice_is_not_doubled_up(self):
        self.build_workbook(self.workbook)

        loading.DataLoader(account=self.account_a, input_file=self.workbook)
        loading.DataLoader(account=self.account_a, input_file=self.workbook)

        # Reference tables are unique per portfolio, and the buy is matched on its legacy_id.
        self.assertEqual(Market.objects.filter(account=self.account_a).count(), 1)
        self.assertEqual(Instrument.objects.filter(account=self.account_a).count(), 1)
        self.assertEqual(Buy.objects.filter(account=self.account_a).count(), 1)
        self.assertEqual(Parcel.objects.filter(account=self.account_a).count(), 1)

    def test_a_row_with_no_legacy_id_is_always_added(self):
        buy = {
            'instrument__name': 'BHP',
            'date': date(2023, 7, 1),
            'quantity': Decimal('100'),
            'unit_price': Decimal('40'),
            'unit_price_currency': 'AUD',
            'total_brokerage': Decimal('10'),
            'total_brokerage_currency': 'AUD',
        }
        self.build_workbook(self.workbook, buys=[buy])

        loading.DataLoader(account=self.account_a, input_file=self.workbook)
        loading.DataLoader(account=self.account_a, input_file=self.workbook)

        self.assertEqual(Buy.objects.filter(account=self.account_a).count(), 2)

    def test_records_are_not_moved_between_portfolios(self):
        self.build_workbook(self.workbook)
        loading.DataLoader(account=self.account_a, input_file=self.workbook)

        market = Market.objects.get(account=self.account_a)

        # An export carries the id of every row, so pointing one at another portfolio would
        # otherwise move the records rather than copy them.
        reparenting_workbook = Path(self.temp_dir.name) / 'export.xlsx'
        self.build_workbook(reparenting_workbook, market_ids=[str(market.id)])

        with self.assertRaises(ValueError) as raised:
            loading.DataLoader(account=self.account_b, input_file=reparenting_workbook)

        self.assertIn('Portfolio A', str(raised.exception))
        market.refresh_from_db()
        self.assertEqual(market.account_id, self.account_a.id)

    def test_a_file_that_fails_part_way_leaves_nothing_behind(self):
        # The buy names an instrument the file never lists, which fails after the reference tables
        # have already been written.
        self.build_workbook(
            self.workbook,
            buys=[{
                'legacy_id': 'buy-1',
                'instrument__name': 'NOT_LISTED',
                'date': date(2023, 7, 1),
                'quantity': Decimal('100'),
                'unit_price': Decimal('40'),
                'unit_price_currency': 'AUD',
                'total_brokerage': Decimal('10'),
                'total_brokerage_currency': 'AUD',
            }],
        )

        with self.assertRaises(Instrument.DoesNotExist):
            loading.DataLoader(account=self.account_a, input_file=self.workbook)

        self.assertEqual(Market.objects.filter(account=self.account_a).count(), 0)
        self.assertEqual(Instrument.objects.filter(account=self.account_a).count(), 0)
        self.assertEqual(Buy.objects.filter(account=self.account_a).count(), 0)


class AccountUniquenessTests(TestCase):
    """Portfolios are found by name when loading a file, so a duplicate name would be ambiguous."""

    def test_one_owner_cannot_have_two_portfolios_of_the_same_name(self):
        owner = create_user(username='duplicate-owner')
        fy_type = create_fiscal_year_type()
        Account.objects.create(owner=owner, description='Shared name', fiscal_year_type=fy_type)

        with self.assertRaises(IntegrityError):
            Account.objects.create(owner=owner, description='Shared name', fiscal_year_type=fy_type)

    def test_different_owners_may_use_the_same_name(self):
        fy_type = create_fiscal_year_type()
        first = create_user(username='first-owner')
        second = create_user(username='second-owner')

        Account.objects.create(owner=first, description='My Portfolio', fiscal_year_type=fy_type)
        Account.objects.create(owner=second, description='My Portfolio', fiscal_year_type=fy_type)

        self.assertEqual(Account.objects.filter(description='My Portfolio').count(), 2)


# =============================================================================
# Version and update check
# =============================================================================


class VersionTests(TestCase):

    def test_parse_version_accepts_a_tag(self):
        self.assertEqual(version.parse_version('v1.2.3'), (1, 2, 3))
        self.assertEqual(version.parse_version('1.2.3'), (1, 2, 3))

    def test_parse_version_ignores_anything_else(self):
        self.assertIsNone(version.parse_version(None))
        self.assertIsNone(version.parse_version(''))
        self.assertIsNone(version.parse_version('not-a-version'))

    def test_an_unreachable_github_reports_no_update(self):
        with patch.object(version.requests, 'get', side_effect=OSError('no network')):
            with patch.object(version, 'read_cache', return_value=None):
                result = version.check_for_update()

        self.assertFalse(result['update_available'])
        self.assertIsNone(result['latest_version'])
        self.assertEqual(result['current_version'], version.__version__)

    def test_no_published_release_reports_no_update(self):
        response = MagicMock(status_code=404)
        with patch.object(version.requests, 'get', return_value=response):
            with patch.object(version, 'read_cache', return_value=None):
                with patch.object(version, 'write_cache', side_effect=lambda latest_version, release_url: {
                        'latest_version': latest_version, 'release_url': release_url}):
                    result = version.check_for_update()

        self.assertFalse(result['update_available'])
        self.assertIsNone(result['latest_version'])

    def test_a_newer_release_is_reported(self):
        cached = {'latest_version': '99.0.0', 'release_url': 'https://example.invalid/releases/99.0.0'}
        with patch.object(version, 'read_cache', return_value=cached):
            result = version.check_for_update()

        self.assertTrue(result['update_available'])
        self.assertEqual(result['latest_version'], '99.0.0')
        self.assertEqual(result['release_url'], 'https://example.invalid/releases/99.0.0')

    def test_the_installed_release_is_not_reported_as_an_update(self):
        cached = {'latest_version': version.__version__, 'release_url': 'https://example.invalid/'}
        with patch.object(version, 'read_cache', return_value=cached):
            result = version.check_for_update()

        self.assertFalse(result['update_available'])


class ImportTemplateCommandTests(TestCase):
    """The generated template has to stay loadable by the loader it is generated for."""

    def test_the_template_round_trips_as_an_empty_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / 'blank.xlsx'
            call_command('make_import_template', output=str(output_path))

            tables = excelinterface.get_all_tables_in_excel(output_path)

        for model in make_import_template.TEMPLATE_MODELS:
            # The loader finds each table by the model's own name.
            self.assertIn(model.__name__, tables)
            self.assertEqual(len(tables[model.__name__]), 0)

    def test_the_template_offers_no_column_the_app_owns(self):
        for model in make_import_template.TEMPLATE_MODELS:
            columns = make_import_template.get_template_columns(model)
            for owned in ('id', 'account', 'account_id', 'created_at', 'updated_at', 'current_unit_price'):
                self.assertNotIn(owned, columns, f'{model.__name__} should not offer {owned}')
            self.assertFalse([column for column in columns if column.startswith('calculated_')])
