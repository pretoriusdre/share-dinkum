"""
Comprehensive test suite for share_dinkum_app.

Run with: python manage.py test share_dinkum_app
"""
import io
import json
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
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
    Distribution,
    DataExport,
    CGTReturnSnapshot,
    CGTReturnSnapshotRow,
    AttributionStatement,
    AttributionComponent,
    ResidencyPeriod,
    CPIIndex,
    InstrumentValuation,
    CapitalLossCarryForward,
)
from share_dinkum_app.utils.currency import add_currencies
from share_dinkum_app.utils.filefield_operations import user_directory_path, process_filefield
from share_dinkum_app.decorators import safe_property
from share_dinkum_app.reports import (
    RealisedCapitalGainReport,
    OpenParcelReport,
    CGTBasisChangeReport,
    CGTEventReport,
    CGTScheduleReport,
)
from django.apps import apps
from share_dinkum_app import choices
from share_dinkum_app.choices import CGTAssetCategory
from share_dinkum_app import cgt, constants, excelinterface, loading, version, yfinanceinterface
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


def create_account(owner=None, currency=DEFAULT_CURRENCY, description='Test Account',
                   fy_type=None):
    if owner is None:
        owner = create_user()
    if fy_type is None:
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
# Reports: golden master
# =============================================================================


def create_golden_master_portfolio():
    """A fixed portfolio exercising every mechanism that feeds a capital gains figure.

    Deliberately covers, in one account: buys in two fiscal years, a share split, an AMIT
    cost base adjustment allocated across parcels, a partial sell that bifurcates a parcel,
    a later sell spanning two parcels, and both a gain and a loss.

    The amounts asserted against this fixture are a characterisation of what the app
    computes *today*. They are not independently derived tax answers. Their purpose is to
    make any change in a capital gains figure visible and deliberate: if a test here fails,
    a number that feeds a tax return has moved, and the diff must be explained before the
    expected value is updated.
    """
    account = create_account()
    instrument = create_instrument(account=account, name='GMT')

    buy_one = Buy.objects.create(
        account=account, instrument=instrument, date=date(2022, 8, 15),
        quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
        total_brokerage=Money(Decimal('19.95'), 'AUD'),
    )
    buy_two = Buy.objects.create(
        account=account, instrument=instrument, date=date(2023, 2, 20),
        quantity=Decimal('500'), unit_price=Money(Decimal('12.00'), 'AUD'),
        total_brokerage=Money(Decimal('9.50'), 'AUD'),
    )

    # One-for-two split: every parcel doubles, unit prices halve.
    share_split = ShareSplit.objects.create(
        account=account, instrument=instrument, date=date(2023, 9, 1),
        quantity_before=Decimal('1'), quantity_after=Decimal('2'),
    )

    # Bought part way through FY2024, and after the split so it is never doubled. This
    # parcel is what exercises the cost base allocation weighting: allocate_cost_base_
    # adjustment bounds days held at the sale date but not at the buy date, so this parcel
    # currently receives a full year's weight despite being held for two months.
    buy_three = Buy.objects.create(
        account=account, instrument=instrument, date=date(2024, 5, 1),
        quantity=Decimal('200'), unit_price=Money(Decimal('7.00'), 'AUD'),
        total_brokerage=Money(Decimal('9.50'), 'AUD'),
    )

    # AMIT cost base increase, spread across parcels by quantity x days held.
    adjustment = CostBaseAdjustment.objects.create(
        account=account, instrument=instrument,
        financial_year_end_date=date(2024, 6, 30),
        cost_base_increase=Money(Decimal('150.00'), 'AUD'),
        allocation_method='QTY_HELD',
    )

    # Partial sell at a gain; bifurcates the first parcel.
    sell_gain = Sell.objects.create(
        account=account, instrument=instrument, date=date(2024, 3, 10),
        quantity=Decimal('1500'), unit_price=Money(Decimal('8.00'), 'AUD'),
        total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
    )
    # Later sell at a loss, in the next fiscal year, spanning two parcels.
    sell_loss = Sell.objects.create(
        account=account, instrument=instrument, date=date(2024, 11, 5),
        quantity=Decimal('1000'), unit_price=Money(Decimal('4.00'), 'AUD'),
        total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
    )

    # So OpenParcelReport produces market values rather than nulls.
    instrument.current_unit_price = Decimal('5.5000')
    instrument.save()

    return {
        'account': account, 'instrument': instrument,
        'buy_one': buy_one, 'buy_two': buy_two, 'buy_three': buy_three,
        'share_split': share_split, 'adjustment': adjustment,
        'sell_gain': sell_gain, 'sell_loss': sell_loss,
    }


class CGTGoldenMasterTests(TransactionTestCase):
    """Characterisation of every figure the app currently derives for capital gains.

    These assertions are a record of current behaviour, not independently derived tax
    answers. If one fails, a number that feeds a tax return has moved. Establish why,
    decide whether the movement is correct, and only then update the expected value.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def _parcels(self):
        """Parcels carrying a cost base, oldest buy first, excluding superseded ones.

        A split or a bifurcation replaces a parcel rather than mutating it, so the table
        also holds deactivated shells with a zero cost base. Those are an implementation
        detail and are deliberately not pinned.
        """
        return [
            p for p in Parcel.objects.filter(account=self.account).order_by('buy__date', 'id')
            if p.remaining_quantity or p.sale_date
        ]

    def test_parcel_cost_bases(self):
        parcels = self._parcels()
        self.assertEqual(len(parcels), 5)

        expected = [
            # buy date,        quantity, unit cost base,       total cost base,      sale date
            (date(2022, 8, 15), '1500', '5.059425533333333333333333333', '7589.1383', date(2024, 3, 10)),
            (date(2022, 8, 15), '500',  '5.0594256', '2529.7128', date(2024, 11, 5)),
            (date(2023, 2, 20), '500',  '6.0589506', '3029.4753', date(2024, 11, 5)),
            (date(2023, 2, 20), '500',  '6.0589504', '3029.4752', None),
            (date(2024, 5, 1),  '200',  '7.055742',  '1411.1484', None),
        ]
        for parcel, (buy_date, qty, unit_cb, total_cb, sale_date) in zip(parcels, expected):
            self.assertEqual(parcel.buy.date, buy_date)
            self.assertEqual(parcel.parcel_quantity, Decimal(qty))
            self.assertEqual(parcel.unit_cost_base.amount, Decimal(unit_cb))
            self.assertEqual(parcel.total_cost_base.amount, Decimal(total_cb))
            self.assertEqual(parcel.sale_date, sale_date)

    def test_cost_base_adjustment_allocation(self):
        """Pins how the $150 AMIT adjustment is spread across parcels.

        Weighted by quantity x days actually held during the year, bounded at both ends.
        The 2024-05-01 parcel was held for 61 days of a 366 day fiscal year, so it takes
        61/366 of a full year's weight per unit.

        This previously gave that parcel a full year's weight -- $9.375 rather than $1.648
        -- because only the sale side was bounded. Correcting it moved cost base off the
        recently bought parcel and onto the ones held throughout, which is why the two
        older parcels are now higher.
        """
        allocations = [p.total_adjustments.amount for p in self._parcels()]
        self.assertEqual(
            allocations,
            [Decimal('74.1758'), Decimal('24.7253'),
             Decimal('24.7253'), Decimal('24.7252'), Decimal('1.6484')],
        )
        # The whole adjustment is allocated, exactly: none lost to rounding, none
        # duplicated. The largest parcel absorbs the residual where the weights do not
        # divide evenly, and a bifurcated allocation gives its remainder the difference
        # rather than the complementary fraction.
        self.assertEqual(sum(allocations), Decimal('150.00'))

    def test_a_parcel_bought_late_in_the_year_gets_a_proportionate_share(self):
        """The correction, stated as a rule rather than as a set of figures."""
        parcels = {p.buy.date: p for p in self._parcels()}
        held_all_year = parcels[date(2023, 2, 20)]
        held_61_days = parcels[date(2024, 5, 1)]

        per_unit_full = held_all_year.total_adjustments.amount / held_all_year.parcel_quantity
        per_unit_part = held_61_days.total_adjustments.amount / held_61_days.parcel_quantity

        # 61 of 366 days, so a sixth of the per unit share of a parcel held throughout.
        # Not exact: each allocation is rounded to the cost base column's four decimal
        # places, which on a $1.65 share is a relative difference of a few parts in 100,000.
        self.assertAlmostEqual(
            float(per_unit_part / per_unit_full), 61 / 366, places=4)

    def test_realised_capital_gain_report(self):
        df = RealisedCapitalGainReport(account=self.account).generate()
        self.assertEqual(len(df), 3)

        expected = [
            # sell date,       qty,    days, proceeds,   cost base,              gain,                    fiscal year
            (date(2024, 3, 10), '1500', 573, '11990.50', '7589.1383', '4401.3617',  'FY2023/24'),
            (date(2024, 11, 5), '500',  813, '1995.25',  '2529.7128', '-534.4628',  'FY2024/25'),
            (date(2024, 11, 5), '500',  624, '1995.25',  '3029.4753', '-1034.2253', 'FY2024/25'),
        ]
        for (_, row), (sell_date, qty, days, proceeds, cost_base, gain, fy) in zip(df.iterrows(), expected):
            self.assertEqual(row['sell_date'], sell_date)
            self.assertEqual(row['quantity_sold'], Decimal(qty))
            self.assertEqual(row['days_held'], days)
            self.assertEqual(row['proceeds'].amount, Decimal(proceeds))
            self.assertEqual(row['cost_base'].amount, Decimal(cost_base))
            self.assertEqual(row['capital_gain'].amount, Decimal(gain))
            self.assertEqual(str(row['fiscal_year']), fy)

        # Both a gain and a loss are represented, so sign handling is exercised.
        gains = [row['capital_gain'].amount for _, row in df.iterrows()]
        self.assertTrue(any(g > 0 for g in gains))
        self.assertTrue(any(g < 0 for g in gains))

    def test_open_parcel_report(self):
        df = OpenParcelReport(account=self.account).generate()
        self.assertEqual(len(df), 2)

        expected = [
            # buy date,       qty,   unit cost base,        cost base,              market value, unrealised gain
            (date(2023, 2, 20), '500', '6.0589504', '3029.4752', '2750.00', '-279.4752'),
            (date(2024, 5, 1),  '200', '7.055742',  '1411.1484', '1100.00', '-311.1484'),
        ]
        for (_, row), (buy_date, qty, unit_cb, cost_base, value, gain) in zip(df.iterrows(), expected):
            self.assertEqual(row['buy_date'], buy_date)
            self.assertEqual(row['remaining_quantity'], Decimal(qty))
            self.assertEqual(row['unit_cost_base'].amount, Decimal(unit_cb))
            self.assertEqual(row['cost_base'].amount, Decimal(cost_base))
            self.assertEqual(row['current_value'].amount, Decimal(value))
            self.assertEqual(row['unrealised_gain'].amount, Decimal(gain))

        self.assertAlmostEqual(df.iloc[0]['unrealised_gain_pct'], -0.0922520177752239, places=12)
        self.assertAlmostEqual(df.iloc[1]['unrealised_gain_pct'], -0.22049303956975752, places=12)

    def test_quantities_reconcile(self):
        """Everything bought is either still held or accounted for in an allocation."""
        bought = Decimal('1000') * 2 + Decimal('500') * 2 + Decimal('200')   # post-split
        sold = sum(a.quantity for a in SellAllocation.objects.filter(account=self.account))
        held = sum(p.remaining_quantity for p in self._parcels())
        self.assertEqual(sold, Decimal('2500'))
        self.assertEqual(held, Decimal('700'))
        self.assertEqual(sold + held, bought)


class CGTEventTests(TransactionTestCase):
    """The cgt package must re-derive today's figures exactly, not merely approximately."""

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def test_events_match_the_realised_capital_gain_report(self):
        """Same rows, same figures, as the report the app has always produced.

        This is the load bearing test for the whole package: if it passes, the calculation
        can be moved without moving a number.
        """
        events = cgt.disposal_events(self.account)
        df = RealisedCapitalGainReport(account=self.account).generate()

        self.assertEqual(len(events), len(df))

        by_allocation = {str(row['sell_allocation_id']): row for _, row in df.iterrows()}
        for event in events:
            row = by_allocation[str(event.sell_allocation_id)]
            self.assertEqual(event.instrument, row['instrument'])
            self.assertEqual(event.quantity, row['quantity_sold'])
            self.assertEqual(event.days_held, row['days_held'])
            self.assertEqual(event.purchase_date, row['buy_date'])
            self.assertEqual(event.event_date, row['sell_date'])
            self.assertEqual(event.fiscal_year, row['fiscal_year'])
            self.assertEqual(event.cost_base.amount, row['cost_base'].amount)
            self.assertEqual(event.net_proceeds.amount, row['proceeds'].amount)
            self.assertEqual(event.capital_gain.amount, row['capital_gain'].amount)

    def test_cost_base_components_sum_to_the_cost_base(self):
        """Purchase, brokerage and adjustments must account for the whole cost base."""
        for event in cgt.disposal_events(self.account):
            components = (
                event.buy_consideration.amount
                + event.buy_brokerage.amount
                + event.cost_base_adjustments.amount
            )
            self.assertEqual(
                components, event.cost_base.amount,
                f'components do not rebuild the cost base for {event.sell_allocation_id}')

    def test_gross_gain_and_loss_split_the_signed_gain(self):
        for event in cgt.disposal_events(self.account):
            self.assertGreaterEqual(event.gross_gain.amount, 0)
            self.assertGreaterEqual(event.gross_loss.amount, 0)
            # Exactly one side carries the value.
            self.assertTrue(event.gross_gain.amount == 0 or event.gross_loss.amount == 0)
            self.assertEqual(
                event.gross_gain.amount - event.gross_loss.amount, event.capital_gain.amount)

    def test_fiscal_year_filter(self):
        all_events = cgt.disposal_events(self.account)
        self.assertEqual(len(all_events), 3)
        fy2024 = cgt.disposal_events(self.account, fiscal_year='FY2023/24')
        self.assertEqual(len(fy2024), 1)
        fy2025 = cgt.disposal_events(self.account, fiscal_year='FY2024/25')
        self.assertEqual(len(fy2025), 2)

    def test_characterisation_matches_todays_rules(self):
        for event in cgt.disposal_events(self.account):
            # Everything in the fixture is held well over a year, and sold before 2027.
            self.assertEqual(event.method, 'discount')
            self.assertEqual(event.discount_percentage, Decimal('0.5'))
            self.assertEqual(event.regime, 'pre_2027')

    def test_events_are_immutable(self):
        event = cgt.disposal_events(self.account)[0]
        with self.assertRaises(Exception):
            event.capital_gain = Money(Decimal('1'), 'AUD')

    def test_empty_account_yields_no_events(self):
        # Reuses the existing owner and fiscal year type: both carry unique constraints, so
        # create_account() cannot be called twice within one test.
        empty = Account.objects.create(
            owner=self.account.owner,
            description='Empty',
            currency=DEFAULT_CURRENCY,
            fiscal_year_type=self.account.fiscal_year_type,
        )
        self.assertEqual(cgt.disposal_events(empty), [])


class CGTClassificationTests(TransactionTestCase):
    """Deriving the CGT schedule category from what an instrument legally is."""

    def setUp(self):
        self.account = create_account()

    def _instrument(self, name, legal_form, market_code='ASX', suffix='AX',
                    country=None, listed=True):
        market, _ = Market.objects.get_or_create(
            account=self.account, code=market_code,
            defaults={'suffix': suffix},
        )
        if country is not None or not listed:
            market.country = country
            market.is_exchange_listed = listed
            market.save()
        instrument = Instrument.objects.create(
            account=self.account, market=market, name=name, currency=DEFAULT_CURRENCY)
        instrument.legal_form = legal_form
        instrument.legal_form_source = 'USER'
        instrument.save()
        return instrument

    def test_australian_listed_company_is_shares(self):
        instrument = self._instrument('AFI', 'COMPANY')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.AU_LISTED_SHARES)

    def test_australian_listed_trust_is_units(self):
        instrument = self._instrument('VAS', 'UNIT_TRUST')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.AU_LISTED_UNITS)

    def test_currency_and_holdings_do_not_decide_the_category(self):
        """VGS is AUD quoted on the ASX yet holds only foreign shares.

        The CGT asset is the unit in the Australian trust, not what the trust owns, so this
        belongs with Australian listed units. Deriving the category from currency or from
        the fund's holdings would put it in the wrong box.
        """
        instrument = self._instrument('VGS', 'UNIT_TRUST')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.AU_LISTED_UNITS)

    def test_foreign_listed_fund_is_other_units(self):
        instrument = self._instrument('VWRA', 'UNIT_TRUST', market_code='LSE', suffix='L')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.OTHER_UNITS)

    def test_foreign_listed_company_is_other_shares(self):
        instrument = self._instrument('AAPL', 'COMPANY', market_code='NASDAQ', suffix='')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.OTHER_SHARES)

    def test_stapled_securities_are_units(self):
        instrument = self._instrument('SCG', 'STAPLED')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.AU_LISTED_UNITS)

    def test_unlisted_australian_company_is_other_shares(self):
        instrument = self._instrument(
            'PRIV', 'COMPANY', market_code='UNLISTED', suffix='', country='AU', listed=False)
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.OTHER_SHARES)

    def test_rights_and_debt_fall_to_other_assets(self):
        self.assertEqual(self._instrument('CTDR', 'RIGHT_OPTION').cgt_asset_category, CGTAssetCategory.OTHER_ASSETS)
        self.assertEqual(self._instrument('BOND', 'DEBT').cgt_asset_category, CGTAssetCategory.OTHER_ASSETS)

    def test_real_property_is_categorised_by_where_the_land_is(self):
        au = self._instrument('LAND', 'REAL_PROPERTY')
        self.assertEqual(au.cgt_asset_category, CGTAssetCategory.AU_REAL_ESTATE)
        overseas = self._instrument(
            'VILLA', 'REAL_PROPERTY', market_code='LSE', suffix='L')
        self.assertEqual(overseas.cgt_asset_category, CGTAssetCategory.OVERSEAS_REAL_ESTATE)
        self.assertTrue(cgt.is_real_property(au))

    def test_unclassified_is_never_guessed_into_a_category(self):
        market = Market.objects.create(account=self.account, code='ASX', suffix='AX')
        instrument = Instrument.objects.create(
            account=self.account, market=market, name='WHAT', currency=DEFAULT_CURRENCY)
        self.assertEqual(instrument.legal_form, 'UNKNOWN')
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.UNCLASSIFIED)
        self.assertFalse(instrument.is_classified)

    def test_an_override_wins_over_the_derived_category(self):
        instrument = self._instrument('ODD', 'COMPANY')
        instrument.cgt_asset_category_override = CGTAssetCategory.COLLECTABLES
        instrument.save()
        self.assertEqual(instrument.cgt_asset_category, CGTAssetCategory.COLLECTABLES)

    def test_a_suggestion_is_not_a_confirmation(self):
        market = Market.objects.create(account=self.account, code='ASX', suffix='AX')
        instrument = Instrument.objects.create(
            account=self.account, market=market, name='VAS', currency=DEFAULT_CURRENCY)
        # Seeded on creation, so the category resolves...
        self.assertEqual(instrument.legal_form, 'UNIT_TRUST')
        self.assertEqual(instrument.legal_form_source, 'SUGGESTED')
        # ...but it is not treated as settled.
        self.assertFalse(instrument.is_classified)


class InstrumentClassificationSignalTests(TransactionTestCase):
    """Suggestions on creation must never overwrite what the user has said."""

    def setUp(self):
        self.account = create_account()
        self.market = Market.objects.create(account=self.account, code='ASX', suffix='AX')

    def test_market_country_is_suggested_from_the_suffix(self):
        self.assertEqual(self.market.country, 'AU')
        lse = Market.objects.create(account=self.account, code='LSE', suffix='L')
        self.assertEqual(lse.country, 'GB')

    def test_an_explicit_country_is_not_overwritten(self):
        market = Market.objects.create(
            account=self.account, code='CHIA', suffix='AX', country='NZ')
        self.assertEqual(market.country, 'NZ')

    def test_a_seeded_code_is_suggested(self):
        vas = Instrument.objects.create(
            account=self.account, market=self.market, name='VAS', currency=DEFAULT_CURRENCY)
        self.assertEqual(vas.legal_form, 'UNIT_TRUST')
        afi = Instrument.objects.create(
            account=self.account, market=self.market, name='AFI', currency=DEFAULT_CURRENCY)
        self.assertEqual(afi.legal_form, 'COMPANY')

    def test_an_unknown_code_stays_unknown(self):
        instrument = Instrument.objects.create(
            account=self.account, market=self.market, name='ZZZZ', currency=DEFAULT_CURRENCY)
        self.assertEqual(instrument.legal_form, 'UNKNOWN')
        self.assertEqual(instrument.legal_form_source, 'DEFAULT')

    def test_a_user_answer_survives_a_later_save(self):
        instrument = Instrument.objects.create(
            account=self.account, market=self.market, name='VAS', currency=DEFAULT_CURRENCY)
        instrument.legal_form = 'COMPANY'
        instrument.legal_form_source = 'USER'
        instrument.save()
        instrument.refresh_from_db()
        instrument.save()
        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form, 'COMPANY')
        self.assertEqual(instrument.legal_form_source, 'USER')

    def test_market_data_is_not_trusted_for_equities(self):
        """Yahoo reports EQUITY for stapled securities and property trusts alike.

        That is precisely the distinction the classification exists to make, so an EQUITY
        answer is treated as no answer rather than as a suggestion of COMPANY.
        """
        instrument = Instrument.objects.create(
            account=self.account, market=self.market, name='ZZZZ', currency=DEFAULT_CURRENCY)
        self.assertIsNone(cgt.suggest_legal_form(instrument, {'quoteType': 'EQUITY'}))
        self.assertEqual(cgt.suggest_legal_form(instrument, {'quoteType': 'ETF'}), 'UNIT_TRUST')


class LegalFormFromActivityTests(TransactionTestCase):
    """Inferring what an instrument is from what it has actually paid.

    Stronger evidence than any list of codes, because it comes from the user's own records:
    a company pays dividends, a trust pays distributions, and a stapled security pays both.
    """

    def setUp(self):
        self.account = create_account()
        self.market = Market.objects.create(account=self.account, code='ASX', suffix='AX')

    def _instrument(self, name):
        return Instrument.objects.create(
            account=self.account, market=self.market, name=name, currency=DEFAULT_CURRENCY)

    def _dividend(self, instrument):
        Dividend.objects.create(
            account=self.account, instrument=instrument, date=date(2024, 3, 1),
            quantity=Decimal('100'),
            franked_amount_per_share=Money(Decimal('0.10'), 'AUD'),
            unfranked_amount_per_share=Money(0, 'AUD'),
        )

    def _distribution(self, instrument):
        Distribution.objects.create(
            account=self.account, instrument=instrument, date=date(2024, 3, 1),
            quantity=Decimal('100'),
            distribution_amount_per_share=Money(Decimal('0.10'), 'AUD'),
        )

    def test_dividends_alone_imply_a_company(self):
        instrument = self._instrument('WOW')
        self._dividend(instrument)
        self.assertEqual(cgt.suggest_legal_form_from_activity(instrument), 'COMPANY')

    def test_distributions_alone_imply_a_trust(self):
        instrument = self._instrument('ZZTRUST')
        self._distribution(instrument)
        self.assertEqual(cgt.suggest_legal_form_from_activity(instrument), 'UNIT_TRUST')

    def test_paying_both_implies_a_stapled_security(self):
        """A share and a unit bound together is the only thing that pays both."""
        instrument = self._instrument('ZZSTAPLE')
        self._dividend(instrument)
        self._distribution(instrument)
        self.assertEqual(cgt.suggest_legal_form_from_activity(instrument), 'STAPLED')

    def test_no_income_history_gives_no_answer(self):
        """The honest result for a holding that has never paid anything."""
        self.assertIsNone(cgt.suggest_legal_form_from_activity(self._instrument('ZZQUIET')))

    def test_trust_and_stapled_reach_the_same_schedule_category(self):
        """So confusing the two cannot change a reported figure; confusing either with a
        company can. That asymmetry is why the inference is worth trusting as a suggestion.
        """
        trust = self._instrument('ZZT')
        trust.legal_form, trust.legal_form_source = 'UNIT_TRUST', 'USER'
        trust.save()
        stapled = self._instrument('ZZS')
        stapled.legal_form, stapled.legal_form_source = 'STAPLED', 'USER'
        stapled.save()
        self.assertEqual(trust.cgt_asset_category, stapled.cgt_asset_category)

        company = self._instrument('ZZC')
        company.legal_form, company.legal_form_source = 'COMPANY', 'USER'
        company.save()
        self.assertNotEqual(company.cgt_asset_category, trust.cgt_asset_category)


class CGTDiscountTests(TestCase):
    """The discount rule, isolated from the object graph."""

    def test_held_more_than_365_days_is_eligible(self):
        self.assertTrue(cgt.is_discount_eligible(date(2023, 1, 1), date(2024, 1, 2)))
        self.assertEqual(
            cgt.discount_percentage(date(2023, 1, 1), date(2024, 1, 2)), Decimal('0.5'))

    def test_held_exactly_365_days_is_not_eligible(self):
        self.assertFalse(cgt.is_discount_eligible(date(2023, 1, 1), date(2024, 1, 1)))
        self.assertEqual(
            cgt.discount_percentage(date(2023, 1, 1), date(2024, 1, 1)), Decimal('0'))

    def test_the_anniversary_itself_does_not_qualify(self):
        """The conservative reading of "at least 12 months before" (s115-25(1)).

        Arguably exactly twelve months satisfies it, but the ATO's guidance requires the
        event to fall after the anniversary. Erring this way can only deny a discount, not
        claim one that is unavailable.
        """
        self.assertFalse(cgt.is_discount_eligible(date(2023, 6, 15), date(2024, 6, 15)))
        self.assertTrue(cgt.is_discount_eligible(date(2023, 6, 15), date(2024, 6, 16)))

    def test_29_february_falls_back_to_28_february(self):
        """A leap day has no anniversary in a common year."""
        from share_dinkum_app.cgt.discount import twelve_month_anniversary
        self.assertEqual(twelve_month_anniversary(date(2024, 2, 29)), date(2025, 2, 28))
        self.assertEqual(twelve_month_anniversary(date(2023, 3, 1)), date(2024, 3, 1))
        # Bought on the leap day, sold on 1 March the next year: past 28 February, eligible.
        self.assertTrue(cgt.is_discount_eligible(date(2024, 2, 29), date(2025, 3, 1)))
        self.assertFalse(cgt.is_discount_eligible(date(2024, 2, 29), date(2025, 2, 28)))

    def test_missing_dates_are_not_eligible(self):
        self.assertFalse(cgt.is_discount_eligible(None, date(2024, 1, 1)))
        self.assertFalse(cgt.is_discount_eligible(date(2024, 1, 1), None))

    def test_the_rate_is_exact_not_a_float(self):
        """Guards against rounding noise once the rate multiplies an apportionment."""
        from share_dinkum_app.cgt.discount import FULL_DISCOUNT_RATE
        self.assertIsInstance(FULL_DISCOUNT_RATE, Decimal)
        self.assertEqual(FULL_DISCOUNT_RATE, Decimal('0.5'))

    def test_a_loss_is_never_discounted(self):
        from share_dinkum_app.cgt.discount import apply_discount
        loss = Money(Decimal('-100'), 'AUD')
        self.assertEqual(
            apply_discount(loss, date(2020, 1, 1), date(2024, 1, 1)), loss)


class AttributionStatementTests(TransactionTestCase):
    """Capital gains a trust attributes to a member, from its annual statement.

    Figures throughout are taken from a real Vanguard AMMA statement for the year ended
    30 June 2025, so the gross up arithmetic is checked against a document rather than
    against numbers invented to make it work.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account, name='VGS')
        self.instrument.legal_form = 'UNIT_TRUST'
        self.instrument.legal_form_source = 'USER'
        self.instrument.save()
        self.statement = AttributionStatement.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=date(2025, 6, 30),
        )

    def _component(self, component, amount):
        return AttributionComponent.objects.create(
            account=self.account, statement=self.statement,
            component=component, amount=Money(Decimal(amount), 'AUD'),
        )

    def _real_statement(self):
        """The FY2025 VGS statement: everything NTAP, nothing on the other method."""
        self._component('DISCOUNTED_NTAP', '4845.57')
        self._component('AMIT_GROSS_UP', '4845.57')
        self._component('NET_CAPITAL_GAIN', '4845.57')
        self._component('TOTAL_CY_CG', '9691.14')
        self._component('FOREIGN_SOURCE_INCOME', '9484.75')

    def test_the_grossed_up_total_is_derived_not_transcribed(self):
        self._real_statement()
        self.assertEqual(self.statement.discounted_capital_gain, Decimal('4845.57'))
        self.assertEqual(self.statement.other_method_capital_gain, Decimal('0'))
        # Twice the discounted amount, matching the statement's own stated total.
        self.assertEqual(
            self.statement.total_current_year_capital_gain, Decimal('9691.14'))

    def test_a_statement_that_reconciles_says_so(self):
        self._real_statement()
        self.assertIs(self.statement.reconciles, True)

    def test_a_misread_statement_is_detected(self):
        """Twice discounted plus other method must equal the stated total.

        This check caught real statements whose layout had been misparsed. Without it the
        figures look plausible and are wrong.
        """
        self._component('DISCOUNTED_NTAP', '4845.57')
        self._component('TOTAL_CY_CG', '5000.00')
        self.assertIs(self.statement.reconciles, False)

    def test_no_stated_total_means_nothing_to_check(self):
        self._component('DISCOUNTED_NTAP', '4845.57')
        self.assertIsNone(self.statement.reconciles)

    def test_an_attribution_becomes_a_grossed_up_cgt_event(self):
        self._real_statement()
        events = cgt.attribution_events(self.account)
        self.assertEqual(len(events), 1)

        event = events[0]
        self.assertEqual(event.source, 'trust_attribution')
        # The gross up matters: carrying the trust's halved figure would discount twice.
        self.assertEqual(event.capital_gain.amount, Decimal('9691.14'))
        self.assertEqual(event.method, 'discount')
        self.assertEqual(event.discount_percentage, Decimal('0.5'))
        self.assertEqual(event.tap_status, 'NTAP')
        self.assertEqual(event.event_date, date(2025, 6, 30))
        self.assertEqual(event.asset_category, CGTAssetCategory.AU_LISTED_UNITS)
        self.assertIs(event.source_reconciles, True)
        # The member never held what the trust sold, so there is no holding period.
        self.assertIsNone(event.purchase_date)
        self.assertIsNone(event.days_held)
        self.assertIsNone(event.cost_base)

    def test_discounted_and_other_method_gains_stay_separate(self):
        """They are taxed differently, so netting them would understate the discount."""
        self._component('DISCOUNTED_NTAP', '100.00')
        self._component('OTHER_NTAP', '40.00')
        self._component('TOTAL_CY_CG', '240.00')

        events = {event.method: event for event in cgt.attribution_events(self.account)}
        self.assertEqual(set(events), {'discount', 'other'})
        self.assertEqual(events['discount'].capital_gain.amount, Decimal('200.00'))
        self.assertEqual(events['discount'].discount_percentage, Decimal('0.5'))
        self.assertEqual(events['other'].capital_gain.amount, Decimal('40.00'))
        self.assertEqual(events['other'].discount_percentage, Decimal('0'))

    def test_the_tap_split_is_preserved(self):
        """It decides whether a foreign resident can disregard the gain, and cannot be
        recovered once the two are added together."""
        self._component('DISCOUNTED_TAP', '30.00')
        self._component('DISCOUNTED_NTAP', '70.00')
        event = cgt.attribution_events(self.account)[0]
        self.assertEqual(event.capital_gain.amount, Decimal('200.00'))
        self.assertEqual(event.tap_status, 'mixed')

    def test_a_statement_with_no_capital_gains_produces_no_events(self):
        self._component('FOREIGN_SOURCE_INCOME', '1000.00')
        self.assertEqual(cgt.attribution_events(self.account), [])

    def test_attributions_join_disposals_in_the_combined_view(self):
        self._real_statement()
        Buy.objects.create(
            account=self.account, instrument=self.instrument, date=date(2020, 1, 6),
            quantity=Decimal('100'), unit_price=Money(Decimal('80.00'), 'AUD'),
            total_brokerage=Money(0, 'AUD'),
        )
        Sell.objects.create(
            account=self.account, instrument=self.instrument, date=date(2025, 2, 3),
            quantity=Decimal('40'), unit_price=Money(Decimal('110.00'), 'AUD'),
            total_brokerage=Money(0, 'AUD'), strategy='FIFO',
        )
        combined = cgt.all_events(self.account)
        self.assertEqual(len(combined), 2)
        self.assertEqual(
            [event.source for event in combined], ['disposal', 'trust_attribution'])

    def test_one_statement_per_instrument_per_year(self):
        with self.assertRaises(IntegrityError):
            AttributionStatement.objects.create(
                account=self.account, instrument=self.instrument,
                financial_year_end_date=date(2025, 6, 30),
            )

    def test_a_component_appears_once_per_statement(self):
        self._component('DISCOUNTED_NTAP', '10.00')
        with self.assertRaises(IntegrityError):
            self._component('DISCOUNTED_NTAP', '20.00')


class CostBaseAllocationReconciliationTests(TransactionTestCase):
    """A cost base adjustment must still add up after being split many times over.

    Each partial sale bifurcates a parcel, and every cost base adjustment held against it
    is split to follow. Computing each half as its own fraction of the original loses a
    fraction of a cent per split, and a parcel sold down in slices is split repeatedly, so
    the loss compounds with nothing in the records to explain where it went.

    The amounts here are chosen not to divide evenly, so any rounding shows up.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account, name='RND')
        self.buy = Buy.objects.create(
            account=self.account, instrument=self.instrument, date=date(2022, 5, 3),
            quantity=Decimal('1000'), unit_price=Money(Decimal('3.37'), 'AUD'),
            total_brokerage=Money(Decimal('19.95'), 'AUD'),
        )
        # Chosen to land exactly on a rounding boundary when halved: 49.4505 / 2 is
        # 24.72525, which Django's DecimalField rounds half to even, giving 24.7252 twice
        # and losing a hundredth of a cent. An amount that divides cleanly proves nothing.
        self.adjustment_amount = Decimal('49.4505')
        self.adjustment = CostBaseAdjustment.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=date(2023, 6, 30),
            cost_base_increase=Money(self.adjustment_amount, 'AUD'),
            allocation_method='QTY_HELD',
        )

    def _live_allocation_total(self):
        return sum(
            allocation.cost_base_increase.amount
            for allocation in CostBaseAdjustmentAllocation.objects.filter(
                cost_base_adjustment=self.adjustment, deactivation_date__isnull=True)
        )

    def _live_parcel_quantity(self):
        return sum(
            parcel.parcel_quantity
            for parcel in Parcel.objects.filter(
                account=self.account, deactivation_date__isnull=True)
        )

    def test_allocation_survives_an_even_split_on_a_rounding_boundary(self):
        self.assertEqual(self._live_allocation_total(), self.adjustment_amount)
        # Exactly half the parcel, so each side lands on 24.72525.
        Sell.objects.create(
            account=self.account, instrument=self.instrument, date=date(2023, 9, 1),
            quantity=Decimal('500'), unit_price=Money(Decimal('4.11'), 'AUD'),
            total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
        )
        self.assertEqual(self._live_allocation_total(), self.adjustment_amount)

    def test_allocation_survives_being_split_seven_times(self):
        """Sold down in slices, which is what compounds a per-split rounding loss."""
        for index, quantity in enumerate(['500', '250', '125', '62', '31', '15', '7']):
            Sell.objects.create(
                account=self.account, instrument=self.instrument,
                date=date(2023, 9, 1) + timedelta(days=index * 30),
                quantity=Decimal(quantity), unit_price=Money(Decimal('4.11'), 'AUD'),
                total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
            )
            self.assertEqual(
                self._live_allocation_total(), self.adjustment_amount,
                f'cost base went missing after split {index + 1}')

        # Halving repeatedly is the worst case for a per-split rounding loss.
        self.assertEqual(self._live_parcel_quantity(), Decimal('1000'))

    def test_every_unit_carries_a_share_after_splitting(self):
        """No parcel is left holding units with no share of the adjustment."""
        Sell.objects.create(
            account=self.account, instrument=self.instrument, date=date(2023, 9, 1),
            quantity=Decimal('333'), unit_price=Money(Decimal('4.11'), 'AUD'),
            total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
        )
        parcels = Parcel.objects.filter(
            account=self.account, deactivation_date__isnull=True)
        for parcel in parcels:
            if parcel.parcel_quantity:
                self.assertGreater(
                    parcel.total_adjustments.amount, Decimal('0'),
                    f'parcel of {parcel.parcel_quantity} units has no adjustment share')


class MinCGTSelectionTests(TransactionTestCase):
    """Characterisation of which parcels the MIN_CGT strategy picks, and why.

    MIN_CGT is the only place the app currently applies a CGT discount. It ranks parcels by
    the net gain per unit, halving that gain where the parcel has been held more than 365
    days. These tests pin the resulting selection so the rule can be moved into the cgt
    package, and later corrected, without silently changing which parcels a sale consumes.
    """

    def _account_with_parcels(self):
        """Three parcels: two long held at different cost bases, one recently bought."""
        account = create_account()
        instrument = create_instrument(account=account, name='SEL')
        for buy_date, quantity, price, tag in [
            (date(2020, 1, 15), '100', '5.00', 'old_cheap'),
            (date(2020, 6, 15), '100', '9.00', 'old_dear'),
            (date(2024, 9, 1), '100', '7.00', 'recent'),
        ]:
            Buy.objects.create(
                account=account, instrument=instrument, date=buy_date,
                quantity=Decimal(quantity), unit_price=Money(Decimal(price), 'AUD'),
                total_brokerage=Money(0, 'AUD'),
            )
        return account, instrument

    def _allocated_buy_dates(self, sell):
        return [
            a.parcel.buy.date
            for a in sell.sale_allocation.filter(is_active=True).order_by('parcel__buy__date')
        ]

    def test_min_cgt_prefers_the_parcel_with_the_smallest_discounted_gain(self):
        account, instrument = self._account_with_parcels()
        sell = Sell.objects.create(
            account=account, instrument=instrument, date=date(2024, 10, 1),
            quantity=Decimal('100'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(0, 'AUD'), strategy='MIN_CGT',
        )
        # Per unit: old_cheap gain $5.00 halved to $2.50; old_dear $1.00 halved to $0.50;
        # recent $3.00 undiscounted because it is inside 365 days. old_dear ranks lowest.
        self.assertEqual(self._allocated_buy_dates(sell), [date(2020, 6, 15)])

    def test_the_discount_changes_the_choice(self):
        """Without halving, the recent parcel's $3.00 would beat old_cheap's $5.00.

        Pins that the discount is actually load bearing in the ranking rather than
        incidental: old_cheap is selected second only because its gain is halved.
        """
        account, instrument = self._account_with_parcels()
        sell = Sell.objects.create(
            account=account, instrument=instrument, date=date(2024, 10, 1),
            quantity=Decimal('250'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(0, 'AUD'), strategy='MIN_CGT',
        )
        self.assertEqual(
            self._allocated_buy_dates(sell),
            [date(2020, 1, 15), date(2020, 6, 15), date(2024, 9, 1)],
        )
        allocations = {
            a.parcel.buy.date: a.quantity
            for a in sell.sale_allocation.filter(is_active=True)
        }
        self.assertEqual(allocations[date(2020, 6, 15)], Decimal('100'))  # cheapest first
        self.assertEqual(allocations[date(2020, 1, 15)], Decimal('100'))  # then discounted
        self.assertEqual(allocations[date(2024, 9, 1)], Decimal('50'))    # remainder

    def test_holding_period_no_longer_depends_on_a_leap_day(self):
        """The correction: identical calendar holdings now get identical answers.

        Under the old day count, a 1 March to 1 March holding was eligible when a leap year
        intervened and not otherwise, purely because one contained 366 days and the other
        365. Both are exactly twelve months, and both are now treated the same way.
        """
        # Spans 29 February 2024: 366 days, and formerly eligible on that basis alone.
        across_leap_day = (date(2023, 3, 1), date(2024, 3, 1))
        self.assertEqual((across_leap_day[1] - across_leap_day[0]).days, 366)
        self.assertFalse(cgt.is_discount_eligible(*across_leap_day))

        # The same calendar holding a year later: 365 days, and never eligible.
        no_leap_day = (date(2022, 3, 1), date(2023, 3, 1))
        self.assertEqual((no_leap_day[1] - no_leap_day[0]).days, 365)
        self.assertFalse(cgt.is_discount_eligible(*no_leap_day))

        # A day past the anniversary qualifies in both cases.
        self.assertTrue(cgt.is_discount_eligible(date(2023, 3, 1), date(2024, 3, 2)))
        self.assertTrue(cgt.is_discount_eligible(date(2022, 3, 1), date(2023, 3, 2)))


# =============================================================================
# Reports: snapshots and basis change detection
# =============================================================================


class CGTReturnSnapshotTests(TransactionTestCase):
    """Tests for capturing capital gains figures at a point in time."""

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.fy2024 = FiscalYear.objects.get(
            fiscal_year_type=self.account.fiscal_year_type, start_year=2023)
        self.fy2025 = FiscalYear.objects.get(
            fiscal_year_type=self.account.fiscal_year_type, start_year=2024)

    def test_capture_records_only_the_requested_year(self):
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(snapshot.totals['row_count'], 1)
        self.assertEqual(Decimal(snapshot.totals['total_capital_gain']), Decimal('4401.3617'))

        later = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2025)
        self.assertEqual(len(later.rows), 2)
        self.assertEqual(Decimal(later.totals['total_capital_gain']), Decimal('-1568.6881'))

    def test_captured_figures_keep_their_values(self):
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        row = snapshot.rows[0]
        self.assertEqual(row['cost_base'].amount, Decimal('7589.1383'))
        self.assertEqual(row['capital_gain'].amount, Decimal('4401.3617'))
        self.assertEqual(row['proceeds'].amount, Decimal('11990.5000'))
        self.assertEqual(row['sell_date'], date(2024, 3, 10))
        # The fiscal year belongs to the snapshot, so it is not repeated on every row.
        self.assertEqual(snapshot.fiscal_year.name, 'FY2023/24')

    def test_each_disposal_is_its_own_row(self):
        """They were a single JSON blob, and that put a ceiling on the whole feature.

        The blob had to fit one Excel cell to survive the export and import round trip,
        which is 32,767 characters, or about three hundred rows. Rows are sell allocations
        rather than sales, so one sale spanning twelve parcels is twelve of them, and
        anyone trading actively hit the cap -- at which point capture refused outright.
        """
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        self.assertEqual(snapshot.captured_rows.count(), 1)
        self.assertEqual(
            set(snapshot.rows[0].keys()), set(CGTReturnSnapshot.CAPTURED_FIELDS))
        self.assertFalse(hasattr(snapshot, 'payload'))

    def test_a_large_year_is_captured_rather_than_refused(self):
        """The behaviour the cap used to make impossible."""
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        CGTReturnSnapshotRow.objects.bulk_create([
            CGTReturnSnapshotRow(
                account=self.account, snapshot=snapshot, instrument='BULK',
                sell_date=date(2024, 3, 10), quantity_sold=Decimal('1'),
                days_held=400, proceeds=Money(Decimal('10'), 'AUD'),
                cost_base=Money(Decimal('8'), 'AUD'),
                capital_gain=Money(Decimal('2'), 'AUD'))
            for _ in range(2000)
        ])
        self.assertEqual(snapshot.captured_rows.count(), 2001)
        self.assertEqual(snapshot.totals['row_count'], 2001)

    def test_the_allocation_reference_is_not_a_foreign_key(self):
        """A snapshot has to outlive what it points at.

        A later sale bifurcates a parcel and replaces its allocations. A foreign key would
        either block that with PROTECT or destroy the evidence with CASCADE, and the
        evidence is the entire point.
        """
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        row = snapshot.captured_rows.first()
        self.assertIsNotNone(row.sell_allocation_id)

        SellAllocation.objects.filter(id=row.sell_allocation_id).delete()

        row.refresh_from_db()
        self.assertIsNotNone(row.sell_allocation_id)
        self.assertEqual(row.capital_gain.amount, Decimal('4401.3617'))

    def test_recapturing_same_day_replaces_rather_than_duplicates(self):
        first = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024, taken_at=date(2026, 8, 31))
        second = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024, taken_at=date(2026, 8, 31))
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(CGTReturnSnapshot.objects.filter(account=self.account).count(), 1)

    def test_capturing_a_later_day_adds_to_the_history(self):
        CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024, taken_at=date(2026, 8, 30))
        CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024, taken_at=date(2026, 8, 31))
        self.assertEqual(CGTReturnSnapshot.objects.filter(account=self.account).count(), 2)

    def test_capture_records_the_engine_version(self):
        snapshot = CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        self.assertEqual(snapshot.engine_version, version.__version__)

    def test_a_snapshot_exports_as_two_readable_sheets(self):
        """The constraint that shaped the old format, and no longer binds.

        The figures used to travel as JSON in one cell, which capped a snapshot at what a
        cell holds. As rows they get their own sheet, with a column per figure, and there is
        no cap -- so an export is also something a person can read.
        """
        snapshot = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024, is_lodged=True)

        export = DataExport.objects.create(account=self.account, include_price_history=False)
        export.refresh_from_db()
        tables = excelinterface.get_all_tables_in_excel(Path(export.file.path))

        self.assertIn('CGTReturnSnapshot', tables)
        self.assertIn('CGTReturnSnapshotRow', tables)

        rows = tables['CGTReturnSnapshotRow']
        for column in ('instrument', 'sell_date', 'proceeds', 'cost_base', 'capital_gain'):
            self.assertIn(column, rows.columns)
        self.assertEqual(len(rows), snapshot.captured_rows.count())
        self.assertEqual(
            Decimal(str(rows.iloc[0]['capital_gain'])), Decimal('4401.3617'))

        self.assertEqual(
            Decimal(snapshot.totals['total_capital_gain']), Decimal('4401.3617'))


class CGTBasisChangeReportTests(TransactionTestCase):
    """Tests that a change in a lodged figure is detected and attributed."""

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.fy2024 = FiscalYear.objects.get(
            fiscal_year_type=self.account.fiscal_year_type, start_year=2023)

    def test_no_change_produces_no_rows(self):
        CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)
        df = CGTBasisChangeReport(account=self.account).generate()
        self.assertTrue(df.empty)
        self.assertEqual(
            list(df.columns),
            ['fiscal_year', 'taken_at', 'snapshot_basis', 'current_basis',
             'snapshot_engine_version', 'sell_allocation_id', 'status', 'field',
             'snapshot_value', 'current_value', 'difference'],
        )

    def test_a_changed_cost_base_is_reported_with_its_difference(self):
        CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)

        # Stand in for any correction that moves a cost base: add a further adjustment.
        # What matters is that the report notices and quantifies it, not the cause.
        CostBaseAdjustment.objects.create(
            account=self.account, instrument=self.data['instrument'],
            financial_year_end_date=date(2023, 6, 30),
            cost_base_increase=Money(Decimal('80.00'), 'AUD'),
            allocation_method='QTY_HELD',
        )

        df = CGTBasisChangeReport(account=self.account).generate()
        self.assertFalse(df.empty)

        changed_fields = set(df['field'])
        self.assertIn('cost_base', changed_fields)
        self.assertIn('capital_gain', changed_fields)
        self.assertEqual(set(df['status']), {'CHANGED'})

        cost_base_row = df[df['field'] == 'cost_base'].iloc[0]
        self.assertEqual(cost_base_row['snapshot_value'], Decimal('7589.1383'))
        self.assertGreater(cost_base_row['current_value'], Decimal('7589.1383'))
        self.assertGreater(cost_base_row['difference'], Decimal('0'))

        # A higher cost base must show as a smaller gain, by the same amount.
        gain_row = df[df['field'] == 'capital_gain'].iloc[0]
        self.assertEqual(gain_row['difference'], -cost_base_row['difference'])

    def test_a_new_allocation_is_reported_as_added(self):
        CGTReturnSnapshot.capture(account=self.account, fiscal_year=self.fy2024)

        Sell.objects.create(
            account=self.account, instrument=self.data['instrument'],
            date=date(2024, 4, 2), quantity=Decimal('100'),
            unit_price=Money(Decimal('9.00'), 'AUD'),
            total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
        )

        df = CGTBasisChangeReport(account=self.account).generate()
        added = df[df['status'] == 'ADDED']
        self.assertEqual(len(added), 1)
        self.assertIsNone(added.iloc[0]['snapshot_value'])
        self.assertIsNotNone(added.iloc[0]['current_value'])

    def test_lodged_only_filters_to_lodged_snapshots(self):
        CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fy2024,
            taken_at=date(2026, 8, 30), is_lodged=False)

        CostBaseAdjustment.objects.create(
            account=self.account, instrument=self.data['instrument'],
            financial_year_end_date=date(2023, 6, 30),
            cost_base_increase=Money(Decimal('80.00'), 'AUD'),
            allocation_method='QTY_HELD',
        )

        self.assertFalse(CGTBasisChangeReport(account=self.account).generate().empty)
        self.assertTrue(
            CGTBasisChangeReport(account=self.account, lodged_only=True).generate().empty)


# =============================================================================
# Loading: every model must be importable, not merely exportable
# =============================================================================


class DataLoaderModelCoverageTests(TestCase):
    """Guards the asymmetry between the export and import paths.

    generate_export_file iterates apps.get_app_config(...).get_models(), so a new model is
    exported the moment it is defined. DataLoader.get_model_load_order() is a hand
    maintained list, so a new model is NOT imported unless someone remembers to add it.

    A user who exports a portfolio and imports it back would silently lose every row of the
    forgotten model, with no error. This test fails the moment the two sides diverge.
    """

    def test_every_model_is_in_the_load_order(self):
        from django.apps import apps

        app_models = {m.__name__ for m in apps.get_app_config('share_dinkum_app').get_models()}
        load_order = {m.__name__ for m in loading.DataLoader.get_model_load_order()}

        missing = app_models - load_order
        self.assertEqual(
            missing, set(),
            f"These models are exported but cannot be imported back: {sorted(missing)}. "
            f"Add them to DataLoader.get_model_load_order(), after the models they "
            f"reference by foreign key."
        )

    def test_load_order_contains_no_unknown_models(self):
        from django.apps import apps

        app_models = {m.__name__ for m in apps.get_app_config('share_dinkum_app').get_models()}
        load_order = {m.__name__ for m in loading.DataLoader.get_model_load_order()}

        unknown = load_order - app_models
        self.assertEqual(
            unknown, set(),
            f"The load order names models that no longer exist: {sorted(unknown)}."
        )

    def test_load_order_has_no_duplicates(self):
        names = [m.__name__ for m in loading.DataLoader.get_model_load_order()]
        self.assertEqual(len(names), len(set(names)))

    def test_required_foreign_keys_are_loaded_before_their_dependants(self):
        """A model must not be loaded before something it *requires* by foreign key.

        get_model_load_order() carries a TODO about deriving the order from dependencies;
        until it does, this asserts the hand maintained order is at least self consistent.

        Only non-nullable foreign keys are checked. A nullable one can be filled after the
        row exists, and there is a genuine cycle among them that no ordering can satisfy:
        Account.owner requires an AppUser, while AppUser.default_account points back at an
        Account. That cycle is why AppUser loads first and its default_account is populated
        afterwards by the assign_default_account signal.
        """
        order = list(loading.DataLoader.get_model_load_order())
        position = {m.__name__: i for i, m in enumerate(order)}

        for model in order:
            for field in model._meta.get_fields():
                if not (field.many_to_one or field.one_to_one):
                    continue
                if not hasattr(field, 'related_model') or field.related_model is None:
                    continue
                if getattr(field, 'null', True):
                    continue  # optional: can be set after the row is created
                related = field.related_model
                if related is model or related.__name__ not in position:
                    continue  # self reference, or a model outside the load order
                self.assertLessEqual(
                    position[related.__name__], position[model.__name__],
                    f"{model.__name__} is loaded before {related.__name__}, which it "
                    f"requires via the non-nullable field '{field.name}'."
                )


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


# --- Phase 4: residency, apportionment and the foreign resident disregard ---

def declare(account, status, start, end=None, i1=None):
    """Shorthand for a residency period, since these tests build a lot of them."""
    return ResidencyPeriod.objects.create(
        account=account, status=status, start_date=start, end_date=end,
        i1_election_made=i1,
    )


class ResidencyPeriodValidationTests(TransactionTestCase):
    """A residency history that does not hang together must be refused, not interpreted."""

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account)

    def test_end_before_start_is_refused(self):
        period = ResidencyPeriod(
            account=self.account, status='RESIDENT',
            start_date=date(2020, 1, 1), end_date=date(2019, 1, 1))
        with self.assertRaises(ValidationError):
            period.full_clean()

    def test_overlapping_periods_are_refused(self):
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        clash = ResidencyPeriod(
            account=self.account, status='FOREIGN',
            start_date=date(2020, 1, 1), end_date=date(2022, 1, 1))
        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_overlap_is_refused_even_without_a_form(self):
        """The Excel importer never calls clean(), so save() has to catch this itself.

        Overlap is the check that can be made order independently, which is why it is the
        one that runs on every write: two statuses on one day is wrong however the rows
        arrived.
        """
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        with self.assertRaises(ValidationError):
            ResidencyPeriod.objects.create(
                account=self.account, status='FOREIGN',
                start_date=date(2020, 1, 1), end_date=date(2022, 1, 1))

    def test_a_gap_is_refused(self):
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        later = ResidencyPeriod(
            account=self.account, status='FOREIGN', start_date=date(2021, 8, 1))
        with self.assertRaises(ValidationError):
            later.full_clean()

    def test_contiguous_periods_are_accepted(self):
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        later = ResidencyPeriod(
            account=self.account, status='FOREIGN', start_date=date(2021, 7, 1))
        later.full_clean()
        later.save()
        self.assertEqual(ResidencyPeriod.objects.filter(account=self.account).count(), 2)

    def test_history_must_reach_back_to_the_earliest_purchase(self):
        """The check that replaces defaulting the start to when the account was created.

        A software timestamp is later than most users' earliest buy, so defaulting to it
        would leave every earlier parcel in a fabricated gap, and would assert a residency
        status that the user never gave.
        """
        Buy.objects.create(
            account=self.account, instrument=self.instrument, date=date(2010, 5, 1),
            quantity=Decimal('100'), unit_price=Money(Decimal('10'), 'AUD'),
            total_brokerage=Money(Decimal('9.95'), 'AUD'))
        late = ResidencyPeriod(
            account=self.account, status='RESIDENT', start_date=date(2015, 1, 1))
        with self.assertRaises(ValidationError):
            late.full_clean()

    def test_coverage_problems_reports_what_clean_would_have_refused(self):
        """A history written past the form still has to be visibly incomplete."""
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        ResidencyPeriod.objects.create(
            account=self.account, status='FOREIGN', start_date=date(2021, 8, 1))
        problems = cgt.residency.coverage_problems(self.account)
        self.assertEqual(len(problems), 1)
        self.assertIn('not declared between', problems[0])

    def test_a_sound_history_reports_no_problems(self):
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2021, 6, 30))
        declare(self.account, 'FOREIGN', date(2021, 7, 1))
        self.assertEqual(cgt.residency.coverage_problems(self.account), [])


class ResidencyDayCountingTests(TransactionTestCase):
    """Both endpoints count, and days nobody declared are reported rather than assumed."""

    def setUp(self):
        self.account = create_account()

    def test_both_endpoints_are_counted(self):
        declare(self.account, 'RESIDENT', date(2020, 1, 1))
        self.assertEqual(
            cgt.resident_days(self.account, date(2020, 1, 1), date(2020, 1, 1)), 1)
        self.assertEqual(
            cgt.resident_days(self.account, date(2020, 1, 1), date(2020, 1, 31)), 31)

    def test_days_are_split_between_statuses(self):
        declare(self.account, 'RESIDENT', date(2020, 1, 1), date(2020, 1, 10))
        declare(self.account, 'FOREIGN', date(2020, 1, 11))
        counts = cgt.residency.days_by_status(
            self.account, date(2020, 1, 1), date(2020, 1, 31))
        self.assertEqual(counts, {'RESIDENT': 10, 'FOREIGN': 21})

    def test_undeclared_days_are_counted_separately_not_ignored(self):
        """A silent zero here would hand out a full discount for a period nobody described."""
        declare(self.account, 'RESIDENT', date(2020, 1, 11), date(2020, 1, 20))
        counts = cgt.residency.days_by_status(
            self.account, date(2020, 1, 1), date(2020, 1, 31))
        self.assertEqual(counts, {'RESIDENT': 10, None: 21})

    def test_temporary_residents_count_with_foreign_residents(self):
        declare(self.account, 'TEMPORARY', date(2020, 1, 1), date(2020, 1, 10))
        declare(self.account, 'FOREIGN', date(2020, 1, 11), date(2020, 1, 20))
        declare(self.account, 'RESIDENT', date(2020, 1, 21))
        self.assertEqual(
            cgt.residency.non_resident_days(
                self.account, date(2020, 1, 1), date(2020, 1, 31)), 20)


class ResidencyInertForResidentsTests(TransactionTestCase):
    """The load-bearing test for the whole phase.

    Every existing user is on the undeclared basis today. If declaring an unbroken period
    of Australian residency moved any figure, the feature could not ship without changing
    numbers people have already lodged, and the change would land on exactly the users it
    was not meant for. Resident days equal total days, so 50% x total/total is 50%.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def test_declaring_residency_changes_no_figure(self):
        before = cgt.disposal_events(self.account)
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        after = cgt.disposal_events(self.account)

        self.assertEqual(len(before), 3)
        self.assertEqual(len(after), len(before))

        # Everything except how the answer was arrived at must be identical.
        annotations = {'residency_status', 'discount_basis', 'tap_status', 'is_disregarded',
                       'disregard_reason'}
        for old_event, new_event in zip(before, after):
            for field in cgt.event_fields():
                if field in annotations:
                    continue
                self.assertEqual(
                    getattr(old_event, field), getattr(new_event, field),
                    f'{field} moved when residency was declared')

    def test_the_discount_is_still_a_flat_half(self):
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        eligible = [e for e in cgt.disposal_events(self.account)
                    if e.method == cgt.events.METHOD_DISCOUNT]
        self.assertTrue(eligible)
        for event in eligible:
            self.assertEqual(event.discount_percentage, Decimal('0.5'))

    def test_the_basis_changes_from_assumed_to_declared(self):
        events = cgt.disposal_events(self.account)
        self.assertTrue(all(e.discount_basis == cgt.BASIS_LEGACY for e in events))
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        events = cgt.disposal_events(self.account)
        self.assertTrue(all(e.discount_basis == cgt.BASIS_DIVISION_115 for e in events))
        self.assertTrue(all(e.residency_status == 'RESIDENT' for e in events))

    def test_a_resident_disregards_nothing(self):
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        events = cgt.disposal_events(self.account)
        self.assertTrue(all(not e.is_disregarded for e in events))
        # Still not taxable Australian property. It is residency, not the asset, that makes
        # the gain assessable here.
        self.assertTrue(all(e.tap_status == cgt.tap.NTAP for e in events))


class DiscountApportionmentTests(TransactionTestCase):
    """s115-115, whose three cases give three different answers to the same question."""

    def setUp(self):
        self.account = create_account()

    def test_undeclared_residency_keeps_the_flat_half(self):
        self.assertEqual(
            cgt.discount_percentage(
                date(2015, 1, 1), date(2020, 1, 1), account=self.account),
            Decimal('0.5'))

    def test_acquired_after_8_may_2012_apportions_over_the_whole_holding(self):
        """s115-115(2): every day of the holding is apportionable.

        Resident 1 Jan 2015 to 30 Jun 2017, foreign thereafter, sold 1 Jan 2020.
        912 resident days of 1827, so 49.917898% of the discount survives.
        """
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1))
        self.assertEqual(
            cgt.apportionment_fraction(self.account, date(2015, 1, 1), date(2020, 1, 1)),
            Decimal('0.49917898'))
        self.assertEqual(
            cgt.discount_percentage(
                date(2015, 1, 1), date(2020, 1, 1), account=self.account),
            Decimal('0.249589490'))

    def test_resident_on_8_may_2012_keeps_the_earlier_years_whatever_happened(self):
        """s115-115(3): the discount was not withdrawn retrospectively.

        Bought 1 Jun 2005, abroad for the whole of 2008 to 2010, home again from 2011,
        abroad from 1 Jul 2017, sold 1 Jan 2020. The three years abroad before 8 May 2012
        do not reduce anything: only the 915 days abroad afterwards do, leaving 4413 of
        5328 days.

        Counting actual residency across the whole period instead, which is the obvious
        reading and the wrong one, would count only 3317 days and strip a fifth of the
        discount off years Parliament deliberately left alone.
        """
        declare(self.account, 'RESIDENT', date(2005, 6, 1), date(2007, 12, 31))
        declare(self.account, 'FOREIGN', date(2008, 1, 1), date(2010, 12, 31))
        declare(self.account, 'RESIDENT', date(2011, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1))

        self.assertEqual(
            cgt.apportionment_fraction(self.account, date(2005, 6, 1), date(2020, 1, 1)),
            Decimal('0.82826577'))
        self.assertEqual(
            cgt.discount_percentage(
                date(2005, 6, 1), date(2020, 1, 1), account=self.account),
            Decimal('0.414132885'))

    def test_already_abroad_on_8_may_2012_counts_only_later_resident_days(self):
        """s115-115(6): no protection for the earlier years, because none was being enjoyed.

        Bought 1 Jan 2010, resident for the first two years, abroad from 1 Jan 2012, home
        again from 1 Jan 2015, sold 1 Jan 2020. Only the 1827 Australian resident days
        after 8 May 2012 count, out of 3653: the 730 resident days of 2010 and 2011 are
        thrown away, because on 8 May 2012 this holder was not here to be protected.

        Counting resident days across the whole period would give 2557 of 3653 instead.
        """
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2011, 12, 31))
        declare(self.account, 'FOREIGN', date(2012, 1, 1), date(2014, 12, 31))
        declare(self.account, 'RESIDENT', date(2015, 1, 1))
        self.assertEqual(
            cgt.apportionment_fraction(self.account, date(2010, 1, 1), date(2020, 1, 1)),
            Decimal('0.50013687'))

    def test_absence_entirely_before_8_may_2012_does_not_apportion_at_all(self):
        """s115-105(2)(e) is the switch, and it only looks after 8 May 2012.

        Someone who lived abroad in 2008 and has been resident ever since keeps the whole
        50%, and never reaches the apportionment formula.
        """
        declare(self.account, 'RESIDENT', date(2005, 1, 1), date(2007, 12, 31))
        declare(self.account, 'FOREIGN', date(2008, 1, 1), date(2010, 12, 31))
        declare(self.account, 'RESIDENT', date(2011, 1, 1))
        self.assertEqual(
            cgt.discount_percentage(
                date(2005, 1, 1), date(2020, 1, 1), account=self.account),
            Decimal('0.5'))

    def test_a_wholly_foreign_holding_gets_no_discount(self):
        declare(self.account, 'FOREIGN', date(2014, 1, 1))
        self.assertEqual(
            cgt.discount_percentage(
                date(2015, 1, 1), date(2020, 1, 1), account=self.account),
            Decimal('0'))

    def test_apportionment_never_rescues_a_holding_under_twelve_months(self):
        declare(self.account, 'RESIDENT', date(2015, 1, 1))
        self.assertEqual(
            cgt.discount_percentage(
                date(2019, 6, 1), date(2019, 12, 1), account=self.account),
            Decimal('0'))


class TaxpayerTypeDiscountTests(TransactionTestCase):
    """s115-10 and s115-100: the rate depends on who is making the gain."""

    def setUp(self):
        self.account = create_account()
        self.purchase = date(2015, 1, 1)
        self.sale = date(2020, 1, 1)

    def _rate(self):
        return cgt.discount_percentage(self.purchase, self.sale, account=self.account)

    def test_undeclared_keeps_the_behaviour_the_app_has_always_had(self):
        self.assertEqual(self.account.taxpayer_type, 'UNDECLARED')
        self.assertEqual(self._rate(), Decimal('0.5'))

    def test_an_individual_discounts_a_half(self):
        self.account.taxpayer_type = 'INDIVIDUAL'
        self.assertEqual(self._rate(), Decimal('0.5'))

    def test_a_complying_superannuation_fund_discounts_a_third(self):
        self.account.taxpayer_type = 'SMSF'
        self.assertEqual(self._rate(), Decimal(1) / Decimal(3))

    def test_a_company_gets_no_discount(self):
        """s115-10 does not list companies. Applying 50% here halves a company's tax."""
        self.account.taxpayer_type = 'COMPANY'
        self.assertEqual(self._rate(), Decimal('0'))

    def test_a_superannuation_funds_rate_is_not_apportioned_by_residency(self):
        """s115-105 opens with "you are an individual", so it never reaches a fund."""
        self.account.taxpayer_type = 'SMSF'
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1))
        self.assertEqual(self._rate(), Decimal(1) / Decimal(3))

    def test_an_individuals_rate_is_apportioned(self):
        self.account.taxpayer_type = 'INDIVIDUAL'
        declare(self.account, 'RESIDENT', date(2015, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1))
        self.assertEqual(self._rate(), Decimal('0.249589490'))


class ForeignResidentDisregardTests(TransactionTestCase):
    """s855-10, s768-915 and the s104-165(3) deeming that overrides them."""

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account)

    def _status(self, acquired, sold):
        return cgt.parcel_tap_status(self.account, self.instrument, acquired, sold)

    def test_listed_shares_are_not_taxable_australian_property(self):
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        self.assertEqual(self._status(date(2015, 1, 1), date(2020, 1, 1)), cgt.tap.NTAP)

    def test_undeclared_residency_leaves_the_question_unanswered(self):
        """Not NTAP, and therefore not disregarded. An unknown must not become an answer."""
        self.assertIsNone(self._status(date(2015, 1, 1), date(2020, 1, 1)))
        disregarded, reason = cgt.disregard(self.account, None, date(2020, 1, 1))
        self.assertFalse(disregarded)
        self.assertIsNone(reason)

    def test_a_foreign_resident_disregards_a_gain_on_listed_shares(self):
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1))
        disregarded, reason = cgt.disregard(self.account, cgt.tap.NTAP, date(2020, 1, 1))
        self.assertTrue(disregarded)
        self.assertIn('s855-10', reason)

    def test_a_temporary_resident_disregards_it_too(self):
        declare(self.account, 'TEMPORARY', date(2010, 1, 1))
        disregarded, reason = cgt.disregard(self.account, cgt.tap.NTAP, date(2020, 1, 1))
        self.assertTrue(disregarded)
        self.assertIn('s768-915', reason)

    def test_a_resident_disregards_nothing(self):
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        disregarded, _ = cgt.disregard(self.account, cgt.tap.NTAP, date(2020, 1, 1))
        self.assertFalse(disregarded)

    def test_real_property_is_taxable_australian_property(self):
        declare(self.account, 'FOREIGN', date(2010, 1, 1))
        self.instrument.legal_form = 'REAL_PROPERTY'
        self.assertEqual(self._status(date(2015, 1, 1), date(2020, 1, 1)), cgt.tap.TAP)

    def test_the_instrument_override_wins(self):
        declare(self.account, 'FOREIGN', date(2010, 1, 1))
        self.instrument.is_taxable_australian_property_override = True
        self.assertEqual(self._status(date(2015, 1, 1), date(2020, 1, 1)), cgt.tap.TAP)
        disregarded, _ = cgt.disregard(self.account, cgt.tap.TAP, date(2020, 1, 1))
        self.assertFalse(disregarded)

    def test_a_false_override_suppresses_the_departure_deeming(self):
        """What the name is there to warn about, pinned so it cannot be changed silently.

        A parcel held at departure under an I1 election is deemed taxable Australian
        property. Answering "no" to the override -- true of an ordinary listed share taken
        on its own -- overrules that, and the gain reads as disregarded.
        """
        declare(self.account, 'RESIDENT', date(2000, 1, 1), end=date(2021, 6, 30))
        declare(self.account, 'FOREIGN', date(2021, 7, 1), i1=True)

        held_at_departure = (date(2015, 1, 1), date(2024, 1, 1))
        self.assertEqual(self._status(*held_at_departure), cgt.tap.TAP)

        self.instrument.is_taxable_australian_property_override = False
        self.assertEqual(self._status(*held_at_departure), cgt.tap.NTAP)
        disregarded, _ = cgt.disregard(self.account, cgt.tap.NTAP, date(2024, 1, 1))
        self.assertTrue(disregarded)


class I1ElectionDeemingTests(TransactionTestCase):
    """s104-165(3): choosing to defer the departure gain keeps those assets in the net.

    This is the reason taxable Australian property cannot be a list of tickers. The deeming
    attaches to what was owned on the day of departure, so two parcels of the same
    instrument, bought a month apart either side of that day, get opposite answers.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account)
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1), i1=True)

    def _status(self, acquired, sold=date(2020, 1, 1)):
        return cgt.parcel_tap_status(self.account, self.instrument, acquired, sold)

    def test_a_parcel_held_at_departure_is_deemed_taxable_australian_property(self):
        self.assertEqual(self._status(date(2015, 1, 1)), cgt.tap.TAP)

    def test_a_parcel_bought_after_departure_is_not(self):
        self.assertEqual(self._status(date(2018, 1, 1)), cgt.tap.NTAP)

    def test_the_two_coexist_for_one_instrument(self):
        """Which is precisely what a per instrument TAP list cannot represent."""
        self.assertEqual(self._status(date(2015, 1, 1)), cgt.tap.TAP)
        self.assertEqual(self._status(date(2018, 1, 1)), cgt.tap.NTAP)

    def test_without_the_election_there_is_no_deeming(self):
        """No election means the I1 gain was taxed on departure, so nothing is held over."""
        ResidencyPeriod.objects.filter(account=self.account, status='FOREIGN').update(
            i1_election_made=False)
        self.assertEqual(self._status(date(2015, 1, 1)), cgt.tap.NTAP)

    def test_the_deeming_lapses_on_returning_to_australia(self):
        """s104-165(3) ends it at the earlier of a CGT event or becoming a resident again."""
        ResidencyPeriod.objects.filter(account=self.account, status='FOREIGN').update(
            end_date=date(2019, 6, 30))
        declare(self.account, 'RESIDENT', date(2019, 7, 1))
        self.assertEqual(self._status(date(2015, 1, 1), date(2020, 1, 1)), cgt.tap.NTAP)
        # Sold while still abroad, it would have been caught.
        self.assertEqual(self._status(date(2015, 1, 1), date(2018, 1, 1)), cgt.tap.TAP)

    def _suppressed(self, acquired, sold=date(2020, 1, 1)):
        return cgt.tap.override_suppresses_deeming(
            self.account, self.instrument, acquired, sold)

    def test_a_no_override_on_a_deemed_parcel_is_reported_as_suppressing_it(self):
        """The silent failure this exists to make audible."""
        self.instrument.is_taxable_australian_property_override = False
        self.assertTrue(self._suppressed(date(2015, 1, 1)))

    def test_an_override_agreeing_with_the_derivation_is_not_reported(self):
        """A parcel bought after departure is NTAP anyway, so the override changed nothing.

        Reporting these would raise a warning on every instrument in an ordinary portfolio,
        which is how a warning stops being read.
        """
        self.instrument.is_taxable_australian_property_override = False
        self.assertFalse(self._suppressed(date(2018, 1, 1)))

    def test_an_unset_override_is_not_reported(self):
        self.assertFalse(self._suppressed(date(2015, 1, 1)))

    def test_a_yes_override_is_not_reported(self):
        """True makes a gain assessable. It is the documented use, and it costs nothing."""
        self.instrument.is_taxable_australian_property_override = True
        self.assertFalse(self._suppressed(date(2015, 1, 1)))


class AttributionDisregardTests(TransactionTestCase):
    """s855-40(2) with s276-55: a foreign resident member drops the non-TAP attributions.

    This is the largest single number this phase moves for an ETF holder. A trust attributes
    its own capital gains, mostly on foreign assets it holds, and for a foreign resident
    member almost none of it is assessable in Australia, while the application until now
    reported all of it.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account, name='VGS')
        self.statement = AttributionStatement.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=date(2025, 6, 30))

    def _component(self, component, amount):
        AttributionComponent.objects.create(
            account=self.account, statement=self.statement,
            component=component, amount=Money(Decimal(amount), 'AUD'))

    def test_a_foreign_resident_disregards_a_non_tap_attribution(self):
        self._component('DISCOUNTED_NTAP', '4845.57')
        declare(self.account, 'FOREIGN', date(2010, 1, 1))
        events = cgt.attribution_events(self.account)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.tap_status, cgt.tap.NTAP)
        self.assertTrue(event.is_disregarded)
        self.assertIn('s855-40(2)', event.disregard_reason)
        # Still grossed up and still reported, so the figure is explainable rather than
        # simply missing from the schedule.
        self.assertEqual(event.capital_gain, Money(Decimal('9691.14'), 'AUD'))

    def test_a_tap_attribution_stays_assessable(self):
        self._component('DISCOUNTED_TAP', '100.00')
        declare(self.account, 'FOREIGN', date(2010, 1, 1))
        event = cgt.attribution_events(self.account)[0]
        self.assertEqual(event.tap_status, cgt.tap.TAP)
        self.assertFalse(event.is_disregarded)

    def test_a_resident_disregards_nothing(self):
        self._component('DISCOUNTED_NTAP', '4845.57')
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        self.assertFalse(cgt.attribution_events(self.account)[0].is_disregarded)

    def test_undeclared_residency_disregards_nothing(self):
        """Unchanged from phase 3, which is what makes this safe to ship."""
        self._component('DISCOUNTED_NTAP', '4845.57')
        self.assertFalse(cgt.attribution_events(self.account)[0].is_disregarded)

    def test_the_two_halves_of_a_mixed_statement_are_treated_separately(self):
        """Netting TAP against NTAP would lose the only thing that decides the outcome."""
        self._component('DISCOUNTED_TAP', '100.00')
        self._component('DISCOUNTED_NTAP', '900.00')
        self._component('OTHER_NTAP', '50.00')
        declare(self.account, 'FOREIGN', date(2010, 1, 1))
        events = cgt.attribution_events(self.account)
        self.assertEqual(len(events), 2)
        discounted, other = events
        # The discounted row mixes both, so it is not disregarded on a guessed ratio.
        self.assertEqual(discounted.tap_status, cgt.tap.TAP_MIXED)
        self.assertFalse(discounted.is_disregarded)
        self.assertEqual(other.tap_status, cgt.tap.NTAP)
        self.assertTrue(other.is_disregarded)


class TaxSettingsBannerTests(TransactionTestCase):
    """The dashboard has to say when a figure rests on an assumption."""

    def setUp(self):
        from share_dinkum_app.admin import _tax_settings_warning
        self._warning = _tax_settings_warning

    def test_an_account_with_no_sales_is_left_alone(self):
        account = create_account()
        self.assertIsNone(self._warning(account))

    def test_an_account_with_sales_and_no_declaration_is_warned(self):
        account = create_golden_master_portfolio()['account']
        warning = self._warning(account)
        self.assertIsNotNone(warning)
        self.assertIn('flat 50%', warning)

    def test_declaring_everything_silences_it(self):
        account = create_golden_master_portfolio()['account']
        account.taxpayer_type = 'INDIVIDUAL'
        account.save()
        declare(account, 'RESIDENT', date(2000, 1, 1))
        self.assertIsNone(self._warning(account))

    def test_reviewing_the_settings_silences_it_whatever_was_chosen(self):
        """A banner that cannot be dismissed by answering it teaches people to ignore it."""
        from django.utils import timezone
        account = create_golden_master_portfolio()['account']
        account.tax_settings_reviewed_at = timezone.now()
        account.save()
        self.assertIsNone(self._warning(account))


# --- Phase 5: the 2027 regime ---

def load_test_cpi(quarters=None):
    """CPI rising 10% a year from the first indexable quarter.

    Round numbers, so a factor in an assertion can be read rather than trusted.
    """
    quarters = quarters or {
        date(2027, 7, 1): '100.0',
        date(2027, 10, 1): '102.5',
        date(2028, 1, 1): '105.0',
        date(2028, 4, 1): '107.5',
        date(2028, 7, 1): '110.0',
        date(2029, 1, 1): '115.0',
        date(2030, 1, 1): '125.0',
    }
    for quarter_start, index_number in quarters.items():
        CPIIndex.objects.update_or_create(
            quarter_start_date=quarter_start,
            defaults={'index_number': Decimal(index_number), 'source': 'test'})


def enable_2027_regime(test_case):
    """Turn the rollout gate on for one test.

    It ships off, because the s112-185 apportioning instrument has not been made, so any
    figure for a straddling holding is a projection.
    """
    patcher = patch.object(constants, 'CGT_2027_REGIME_ENABLED', True)
    patcher.start()
    test_case.addCleanup(patcher.stop)


class IndexationFactorTests(TestCase):
    """Subdivision 960-M, and the refusal to invent a missing quarter."""

    def setUp(self):
        load_test_cpi()

    def test_the_factor_is_the_ratio_of_two_quarters(self):
        # Bought at the cutover, sold in the September 2028 quarter: 110.0 over 100.0.
        self.assertEqual(
            cgt.indexation_factor(date(2027, 7, 1), date(2028, 8, 20)),
            Decimal('1.100'))

    def test_indexation_never_reaches_back_before_the_cutover(self):
        """s960-275(1B). An asset bought in 2010 is indexed over one year, not eighteen."""
        self.assertEqual(
            cgt.indexation_factor(date(2010, 1, 1), date(2028, 8, 20)),
            Decimal('1.100'))

    def test_the_factor_is_three_decimal_places(self):
        factor = cgt.indexation_factor(date(2027, 7, 1), date(2027, 11, 1))
        self.assertEqual(factor, Decimal('1.025'))
        self.assertEqual(factor.as_tuple().exponent, -3)

    def test_a_falling_index_does_not_shrink_a_cost_base(self):
        CPIIndex.objects.create(
            quarter_start_date=date(2031, 1, 1), index_number=Decimal('90.0'))
        self.assertEqual(
            cgt.indexation_factor(date(2027, 7, 1), date(2031, 2, 1)),
            Decimal('1.000'))

    def test_a_missing_quarter_raises_rather_than_defaulting(self):
        """The failure this guards is the quiet one.

        Falling back to the latest published quarter understates the cost base and so
        overstates the gain; extrapolating does the reverse. Both look like an answer.
        """
        with self.assertRaises(cgt.IndexationDataUnavailable) as caught:
            cgt.indexation_factor(date(2027, 7, 1), date(2033, 5, 1))
        self.assertIn('2033-04-01', str(caught.exception))

    def test_an_empty_table_raises_too(self):
        CPIIndex.objects.all().delete()
        with self.assertRaises(cgt.IndexationDataUnavailable):
            cgt.indexation_factor(date(2027, 7, 1), date(2028, 8, 20))


class IndexationEligibilityTests(TransactionTestCase):
    """s114-25: a testing period that starts at the cutover, not at acquisition."""

    def setUp(self):
        self.account = create_account()
        load_test_cpi()

    def test_past_non_residency_does_not_disqualify(self):
        """The intuitive reading is that time abroad costs you indexation. It does not.

        The testing period starts on the later of 1 July 2027 and the day of acquisition, so
        someone who lived overseas until 2026 and has been in Australia since is eligible on
        an asset they bought in 2020. Nothing before the cutover is looked at.
        """
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2020, 12, 31))
        declare(self.account, 'FOREIGN', date(2021, 1, 1), date(2026, 6, 30))
        declare(self.account, 'RESIDENT', date(2026, 7, 1))
        self.assertTrue(cgt.is_indexation_eligible(
            self.account, date(2020, 1, 15), date(2030, 1, 15)))

    def test_one_week_abroad_inside_the_testing_period_denies_it_entirely(self):
        """No apportionment and no partial credit, unlike the discount it replaces."""
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2020, 12, 31))
        declare(self.account, 'FOREIGN', date(2021, 1, 1), date(2026, 6, 30))
        declare(self.account, 'RESIDENT', date(2026, 7, 1), date(2028, 2, 29))
        declare(self.account, 'FOREIGN', date(2028, 3, 1), date(2028, 3, 7))
        declare(self.account, 'RESIDENT', date(2028, 3, 8))
        self.assertFalse(cgt.is_indexation_eligible(
            self.account, date(2020, 1, 15), date(2030, 1, 15)))

    def test_a_disposal_before_the_cutover_is_never_indexed(self):
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        self.assertFalse(cgt.is_indexation_eligible(
            self.account, date(2020, 1, 15), date(2026, 1, 15)))

    def test_undeclared_residency_does_not_get_indexation_on_an_assumption(self):
        """Unlike the discount, there is no prior behaviour here to preserve.

        Granting it would inflate a cost base on a guess, so an undeclared account is simply
        not eligible and the schedule says why.
        """
        self.assertFalse(cgt.is_indexation_eligible(
            self.account, date(2020, 1, 15), date(2030, 1, 15)))


def create_cutover_portfolio(sell_date=date(2028, 8, 20), unit_value='15.00', suffix=''):
    """A parcel bought well before the cutover and sold well after it.

    Cost base 10,019.95, market value at the cutover 15,000, net proceeds 19,990.05. Round
    enough that every figure asserted against it can be checked by hand.

    `suffix` makes a second, independent portfolio in the same test. The factories key on a
    username and a fiscal year type description, both unique, so without it a comparison
    against a counterfactual portfolio fails on a constraint rather than on its assertion.
    """
    account = create_account(
        owner=create_user(username=f'cutover{suffix}'),
        description=f'Cutover Account {suffix}'.strip(),
        fy_type=create_fiscal_year_type(description=f'AU Tax Year {suffix}'.strip()),
    )
    instrument = create_instrument(account=account, name='CUT')

    buy = Buy.objects.create(
        account=account, instrument=instrument, date=date(2020, 1, 15),
        quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
        total_brokerage=Money(Decimal('19.95'), 'AUD'),
    )
    InstrumentValuation.objects.create(
        account=account, instrument=instrument,
        valuation_date=date(2027, 6, 30),
        unit_value=Money(Decimal(unit_value), 'AUD'),
        purpose='CUTOVER_2027', source='USER',
    )
    sell = Sell.objects.create(
        account=account, instrument=instrument, date=sell_date,
        quantity=Decimal('1000'), unit_price=Money(Decimal('20.00'), 'AUD'),
        total_brokerage=Money(Decimal('9.95'), 'AUD'), strategy='FIFO',
    )
    load_test_cpi()
    return {'account': account, 'instrument': instrument, 'buy': buy, 'sell': sell}


class DeemedSaleSplitTests(TransactionTestCase):
    """s112-155: one disposal, two gains, taxed under different regimes."""

    def setUp(self):
        self.data = create_cutover_portfolio()
        self.account = self.data['account']
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        enable_2027_regime(self)

    def _events(self):
        return cgt.disposal_events(self.account)

    def test_one_disposal_becomes_two_rows(self):
        events = self._events()
        self.assertEqual(len(events), 2)
        deferred, post = events
        self.assertEqual(deferred.slice, cgt.events.SLICE_PRE_CUTOVER)
        self.assertEqual(post.slice, cgt.events.SLICE_POST_CUTOVER)
        # One sale, so a report can still present them together.
        self.assertEqual(deferred.sell_allocation_id, post.sell_allocation_id)

    def test_the_deferred_gain_is_measured_to_the_cutover_valuation(self):
        deferred = self._events()[0]
        self.assertEqual(deferred.net_proceeds, Money(Decimal('15000.00'), 'AUD'))
        self.assertEqual(deferred.cost_base, Money(Decimal('10019.95'), 'AUD'))
        self.assertEqual(deferred.capital_gain, Money(Decimal('4980.05'), 'AUD'))

    def test_the_deferred_gain_keeps_the_discount(self):
        deferred = self._events()[0]
        self.assertEqual(deferred.method, cgt.events.METHOD_DISCOUNT)
        self.assertEqual(deferred.discount_percentage, Decimal('0.5'))
        self.assertEqual(
            deferred.gain_category, constants.CGT_GAIN_DEFERRED_NON_RESIDENTIAL)

    def test_the_post_cutover_gain_is_indexed_and_undiscounted(self):
        """s110-36(1A) makes indexation mandatory, and s115-20 then denies the discount."""
        post = self._events()[1]
        self.assertEqual(post.indexation_factor, Decimal('1.100'))
        # 15,000 reacquisition cost lifted by 10% inflation.
        self.assertEqual(post.cost_base, Money(Decimal('16500.000'), 'AUD'))
        self.assertEqual(post.capital_gain, Money(Decimal('3490.050'), 'AUD'))
        self.assertEqual(post.discount_percentage, Decimal('0'))
        self.assertEqual(post.method, cgt.events.METHOD_OTHER)
        self.assertEqual(post.gain_category, constants.CGT_GAIN_NON_RESIDENTIAL)

    def test_the_two_slices_sum_to_the_whole_gain_before_indexation_relief(self):
        """The valuation moves gain between the categories; it does not create or destroy it.

        Worth pinning, because it means a user agonising over the market value is choosing
        how their gain is taxed rather than how much of it there is. What does change the
        total is indexation, and the difference here is exactly the relief.
        """
        deferred, post = self._events()
        whole_gain = Decimal('19990.05') - Decimal('10019.95')
        relief = Decimal('16500.000') - Decimal('15000.00')
        self.assertEqual(
            deferred.capital_gain.amount + post.capital_gain.amount,
            whole_gain - relief)

    def test_the_twelve_month_rule_ignores_the_deemed_reacquisition(self):
        """s114-10(9). Sold a month after the cutover, the deferred slice is still discounted.

        Measured from the reacquisition on 1 July 2027 it would have been held for six weeks
        and would lose the discount, which is the trap this provision exists to prevent.
        """
        data = create_cutover_portfolio(sell_date=date(2027, 8, 20), suffix='b')
        declare(data['account'], 'RESIDENT', date(2010, 1, 1))
        deferred = cgt.disposal_events(data['account'])[0]
        self.assertEqual(deferred.method, cgt.events.METHOD_DISCOUNT)
        self.assertEqual(deferred.discount_percentage, Decimal('0.5'))

    def test_without_a_valuation_the_disposal_is_not_split(self):
        InstrumentValuation.objects.all().delete()
        events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].slice, cgt.events.SLICE_WHOLE)
        self.assertIn('No market value recorded', events[0].pending_reason)

    def test_a_missing_cpi_quarter_leaves_the_row_pending(self):
        CPIIndex.objects.all().delete()
        post = self._events()[1]
        self.assertIsNone(post.indexation_factor)
        self.assertIn('No CPI index number', post.pending_reason)
        # And no relief is given: the cost base is the unindexed reacquisition cost.
        self.assertEqual(post.cost_base, Money(Decimal('15000.00'), 'AUD'))

    def test_the_rollout_gate_reverts_to_a_single_unsplit_row(self):
        with patch.object(constants, 'CGT_2027_REGIME_ENABLED', False):
            events = self._events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].slice, cgt.events.SLICE_WHOLE)
        self.assertEqual(events[0].capital_gain, Money(Decimal('9970.10'), 'AUD'))


class ReturnedExpatIndexationTests(TransactionTestCase):
    """The harshest edge in the reform, and the one most likely to be read as a bug.

    Resident now, abroad at some point after 8 May 2012, holding an asset bought before the
    cutover. s112-155(1)(d) denies them the deemed sale because s115-105 applies to them, so
    none of their pre-2027 growth is banked at 50%. Being resident from the cutover, s114-25
    is satisfied, indexation is mandatory under s110-36(1A), and s115-20 then denies the
    discount to a gain worked out on an indexed cost base.

    They end up with neither the discount nor a full indexation history: relief runs only
    from 2027, on growth that mostly happened before it.
    """

    def setUp(self):
        self.data = create_cutover_portfolio()
        self.account = self.data['account']
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2014, 12, 31))
        declare(self.account, 'FOREIGN', date(2015, 1, 1), date(2020, 12, 31))
        declare(self.account, 'RESIDENT', date(2021, 1, 1))
        enable_2027_regime(self)

    def test_there_is_no_deemed_sale(self):
        applies, reason = cgt.deemed_sale_applies(
            self.account, date(2020, 1, 15), date(2028, 8, 20))
        self.assertFalse(applies)
        self.assertIn('s112-155(1)(d)', reason)

    def test_the_gain_stays_a_single_row(self):
        events = cgt.disposal_events(self.account)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].slice, cgt.events.SLICE_WHOLE)

    def test_indexation_applies_and_takes_the_discount_with_it(self):
        event = cgt.disposal_events(self.account)[0]
        self.assertEqual(event.indexation_factor, Decimal('1.100'))
        # The whole original cost base indexed, not a 2027 market value.
        self.assertEqual(event.cost_base, Money(Decimal('11021.945'), 'AUD'))
        self.assertEqual(event.discount_percentage, Decimal('0'))
        self.assertEqual(event.method, cgt.events.METHOD_OTHER)

    def test_the_report_explains_it_rather_than_leaving_it_to_be_discovered(self):
        event = cgt.disposal_events(self.account)[0]
        self.assertIn('s115-105 applies', event.pending_reason)

    def test_they_are_worse_off_than_if_they_had_never_left(self):
        """Stated as a comparison, because the figure alone does not show the cost."""
        caught = cgt.disposal_events(self.account)[0]

        never_left = create_cutover_portfolio(suffix='b')
        declare(never_left['account'], 'RESIDENT', date(2010, 1, 1))
        deferred, post = cgt.disposal_events(never_left['account'])

        taxed_if_caught = caught.capital_gain.amount
        taxed_if_not = (
            deferred.capital_gain.amount * (Decimal('1') - deferred.discount_percentage)
            + post.capital_gain.amount)
        self.assertGreater(taxed_if_caught, taxed_if_not)


class CutoverValuationTests(TransactionTestCase):
    """A valuation is per unit, and has to survive a split to stay meaningful."""

    def setUp(self):
        self.data = create_cutover_portfolio()
        self.account = self.data['account']

    def test_a_parcel_is_valued_from_the_per_unit_figure(self):
        parcel = Parcel.objects.filter(account=self.account).first()
        value, source = parcel.market_value_at(date(2027, 6, 30))
        self.assertEqual(value, Money(Decimal('15000.00'), 'AUD'))
        self.assertEqual(source, 'USER')

    def test_a_later_split_does_not_double_the_valuation(self):
        """The reason valuations are per unit and never per parcel.

        A one-for-two split doubles the units and halves what a unit is worth. A figure
        stored against the parcel would survive the split unchanged and value the holding at
        twice what it was; scaling the per-unit figure keeps the parcel worth the same.
        """
        held = create_instrument(
            account=self.account, market=self.data['instrument'].market, name='HELD')
        Buy.objects.create(
            account=self.account, instrument=held, date=date(2020, 1, 15),
            quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(Decimal('0'), 'AUD'),
        )
        InstrumentValuation.objects.create(
            account=self.account, instrument=held, valuation_date=date(2027, 6, 30),
            unit_value=Money(Decimal('15.00'), 'AUD'), purpose='CUTOVER_2027',
            source='USER',
        )
        ShareSplit.objects.create(
            account=self.account, instrument=held, date=date(2027, 9, 1),
            quantity_before=Decimal('1'), quantity_after=Decimal('2'),
        )

        parcel = [p for p in Parcel.objects.filter(account=self.account, buy__instrument=held)
                  if p.remaining_quantity][0]
        self.assertEqual(parcel.parcel_quantity, Decimal('2000'))

        # 2,000 units at a value recorded when a unit was worth twice as much.
        value, _source = parcel.market_value_at(date(2027, 6, 30))
        self.assertEqual(value, Money(Decimal('15000.00'), 'AUD'))

    def test_a_price_history_close_is_used_where_no_valuation_was_recorded(self):
        InstrumentValuation.objects.all().delete()
        InstrumentPriceHistory.objects.create(
            account=self.account, instrument=self.data['instrument'],
            date=date(2027, 6, 30), open=Decimal('14'), high=Decimal('16'),
            low=Decimal('14'), close=Decimal('15.5'), volume=1000,
            stock_splits=Decimal('0'),
        )
        parcel = Parcel.objects.filter(account=self.account).first()
        value, source = parcel.market_value_at(date(2027, 6, 30))
        self.assertEqual(value, Money(Decimal('15500.0'), 'AUD'))
        self.assertEqual(source, 'PRICE_HISTORY')

    def test_a_recorded_valuation_beats_a_closing_price(self):
        """A user who had to source a value for a suspended holding keeps their answer."""
        InstrumentPriceHistory.objects.create(
            account=self.account, instrument=self.data['instrument'],
            date=date(2027, 6, 30), open=Decimal('14'), high=Decimal('16'),
            low=Decimal('14'), close=Decimal('15.5'), volume=1000,
            stock_splits=Decimal('0'),
        )
        parcel = Parcel.objects.filter(account=self.account).first()
        value, source = parcel.market_value_at(date(2027, 6, 30))
        self.assertEqual(value, Money(Decimal('15000.00'), 'AUD'))
        self.assertEqual(source, 'USER')

    def test_nothing_is_invented_where_there_is_no_price(self):
        InstrumentValuation.objects.all().delete()
        parcel = Parcel.objects.filter(account=self.account).first()
        self.assertEqual(parcel.market_value_at(date(2027, 6, 30)), (None, None))


class DeemedResetDateTests(TransactionTestCase):
    """One mechanism for four provisions that each reset a cost base to market value."""

    def setUp(self):
        self.account = create_account()

    def test_the_cutover_is_always_a_reset_date(self):
        self.assertEqual(
            cgt.deemed_reset_dates(self.account),
            [(date(2027, 7, 1), 'CUTOVER_2027')])

    def test_leaving_australia_without_the_election_adds_one(self):
        """s104-165: CGT event I1 happened and was taxable, so the cost base resets then."""
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1), i1=False)
        self.assertIn((date(2017, 7, 1), 'DEPARTURE'), cgt.deemed_reset_dates(self.account))

    def test_the_i1_election_means_there_is_no_reset(self):
        """The whole point of the choice: the gain is deferred and the cost base untouched."""
        declare(self.account, 'RESIDENT', date(2010, 1, 1), date(2017, 6, 30))
        declare(self.account, 'FOREIGN', date(2017, 7, 1), i1=True)
        self.assertEqual(
            cgt.deemed_reset_dates(self.account),
            [(date(2027, 7, 1), 'CUTOVER_2027')])

    def test_arriving_in_australia_adds_one(self):
        """s855-45: growth from before arrival was never within the Australian net."""
        declare(self.account, 'FOREIGN', date(2010, 1, 1), date(2016, 12, 31))
        declare(self.account, 'RESIDENT', date(2017, 1, 1))
        self.assertIn((date(2017, 1, 1), 'ARRIVAL'), cgt.deemed_reset_dates(self.account))


class CapitalGainScheduleTests(TransactionTestCase):
    """s102-5: netting, ordering, and the difference the order of operations makes."""

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def test_losses_reduce_gains_rather_than_each_disposal_being_floored(self):
        """FY2024/25 in the fixture is two losses and no gains."""
        schedule = cgt.build_schedule(self.account, 'FY2024/25')
        self.assertEqual(schedule.gross_gains.amount, Decimal('0'))
        self.assertEqual(schedule.gross_losses.amount, Decimal('1568.6881'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('0'))
        # Nothing to absorb them, so the whole amount is carried forward.
        self.assertEqual(schedule.losses_carried_forward.amount, Decimal('1568.6881'))

    def test_the_discount_is_applied_after_losses_not_before(self):
        """The order changes the answer, so it is pinned rather than assumed.

        FY2023/24 has a 4,401.3617 discountable gain. Bringing 1,000 of prior year losses
        against it leaves 3,401.3617 to halve, giving 1,700.68085. Discounting first and
        then deducting the loss would give 1,200.68085 -- a difference of 500 on a 1,000
        loss, which is the whole value of the loss.
        """
        schedule = cgt.build_schedule(
            self.account, 'FY2023/24', prior_year_losses=Money(Decimal('1000'), 'AUD'))
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('1000'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('1700.68085'))

    def test_a_year_with_a_gain_and_no_losses_is_simply_discounted(self):
        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        self.assertEqual(schedule.gross_gains.amount, Decimal('4401.3617'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('2200.68085'))

    def test_prior_year_losses_come_from_the_carry_forward_model(self):
        fiscal_year = FiscalYear.objects.first()
        CapitalLossCarryForward.objects.create(
            account=self.account, fiscal_year=fiscal_year,
            amount=Money(Decimal('500'), 'AUD'), is_opening_balance=True,
        )
        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('500'))

    def test_an_opening_balance_is_the_only_way_to_bring_in_earlier_losses(self):
        """Nothing in the transactions implies it, so a new user needs somewhere to say it."""
        loss = CapitalLossCarryForward(
            account=self.account,
            fiscal_year=FiscalYear.objects.first(),
            amount=Money(Decimal('-500'), 'AUD'),
        )
        with self.assertRaises(ValidationError):
            loss.full_clean()

    def test_an_undeclared_account_produces_a_draft_not_a_schedule(self):
        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        self.assertTrue(schedule.is_draft)
        joined = ' '.join(schedule.warnings)
        self.assertIn('Residency has not been declared', joined)
        self.assertIn('does not say who owns this portfolio', joined)

    def test_declaring_everything_clears_the_draft_warnings(self):
        self.account.taxpayer_type = 'INDIVIDUAL'
        self.account.save()
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        self.data['instrument'].legal_form = 'COMPANY'
        self.data['instrument'].legal_form_source = 'USER'
        self.data['instrument'].save()
        self.data['instrument'].market.country = 'AU'
        self.data['instrument'].market.save()

        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        self.assertEqual(schedule.warnings, [])
        self.assertFalse(schedule.is_draft)

    def test_a_statement_disagreeing_on_cost_base_makes_the_schedule_a_draft(self):
        """And it must surface even though the statement attributes no capital gain.

        `_attribution_events` builds an event only where there is a gain, so a nil-gain
        statement produces none at all -- and a nil-gain statement with a large cost base
        movement is the normal shape for a property trust. A check walking the year's events
        would miss exactly the statements this exists to check.
        """
        self.account.taxpayer_type = 'INDIVIDUAL'
        self.account.save()
        declare(self.account, 'RESIDENT', date(2000, 1, 1))

        instrument = self.data['instrument']
        adjustment = CostBaseAdjustment.objects.create(
            account=self.account, instrument=instrument,
            financial_year_end_date=date(2024, 6, 30),
            cost_base_increase=Money(Decimal('100.00'), 'AUD'))
        statement = AttributionStatement.objects.create(
            account=self.account, instrument=instrument,
            financial_year_end_date=date(2024, 6, 30),
            cost_base_adjustment=adjustment)
        AttributionComponent.objects.create(
            account=self.account, statement=statement,
            component='COSTBASE_INCREASE', amount=Money(Decimal('251.84'), 'AUD'))

        self.assertEqual(
            [e for e in cgt.attribution_events(self.account) if e.instrument == instrument.name],
            [], 'this statement should attribute no capital gain at all')

        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        joined = ' '.join(schedule.warnings)
        self.assertTrue(schedule.is_draft)
        self.assertIn('disagree with the cost base adjustment', joined)
        self.assertIn('251.84', joined)
        self.assertIn('100.00', joined)

        AttributionComponent.objects.filter(statement=statement).update(
            amount=Money(Decimal('100.00'), 'AUD'))
        cleared = ' '.join(cgt.build_schedule(self.account, 'FY2023/24').warnings)
        self.assertNotIn('disagree with the cost base adjustment', cleared)

    def test_an_override_suppressing_the_deeming_makes_the_schedule_a_draft(self):
        """A blanket "no" takes every assessable gain to zero, and says nothing.

        That is the shape of failure worth catching here: the schedule is not wrong-looking,
        it is empty, and empty is the correct answer for a foreign resident who left without
        an I1 election. Only the setting distinguishes the two.
        """
        self.account.taxpayer_type = 'INDIVIDUAL'
        self.account.save()
        # Departure after both buys (2022-08-15, 2023-02-20) and before both sells, so the
        # parcels were held at the I1 moment and the deeming is what would catch them.
        declare(self.account, 'RESIDENT', date(2000, 1, 1), date(2023, 6, 30))
        declare(self.account, 'FOREIGN', date(2023, 7, 1), i1=True)

        instrument = self.data['instrument']
        instrument.legal_form = 'COMPANY'
        instrument.legal_form_source = 'USER'
        instrument.is_taxable_australian_property_override = False
        instrument.save()
        instrument.market.country = 'AU'
        instrument.market.save()

        schedule = cgt.build_schedule(self.account, 'FY2023/24')
        self.assertTrue(schedule.is_draft)
        joined = ' '.join(schedule.warnings)
        self.assertIn('the only reason their gains are disregarded', joined)
        self.assertIn(instrument.name, joined)

        instrument.is_taxable_australian_property_override = None
        instrument.save()
        cleared = ' '.join(cgt.build_schedule(self.account, 'FY2023/24').warnings)
        self.assertNotIn('the only reason their gains are disregarded', cleared)


class StatutoryLossOrderingTests(TransactionTestCase):
    """s102-5 Step 1: losses are spent where they are worth least, and that is the law.

    A deferred non-residential gain carries the 50% discount; a plain non-residential gain
    does not. Left to choose, a taxpayer would spend a loss on the undiscounted gain, saving
    twice as much tax. Step 1(a) requires the opposite.
    """

    def setUp(self):
        self.data = create_cutover_portfolio()
        self.account = self.data['account']
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        enable_2027_regime(self)

        # A second holding, bought and sold entirely after the cutover, at a loss, in the
        # same fiscal year as the first sale.
        losing = create_instrument(
            account=self.account, market=self.data['instrument'].market, name='LOSS')
        Buy.objects.create(
            account=self.account, instrument=losing, date=date(2028, 9, 1),
            quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(Decimal('0'), 'AUD'),
        )
        Sell.objects.create(
            account=self.account, instrument=losing, date=date(2029, 1, 15),
            quantity=Decimal('1000'), unit_price=Money(Decimal('8.00'), 'AUD'),
            total_brokerage=Money(Decimal('0'), 'AUD'), strategy='FIFO',
        )

    def _schedule(self):
        year = cgt.disposal_events(self.account)[0].fiscal_year
        return cgt.build_schedule(self.account, year)

    def test_both_categories_are_present(self):
        schedule = self._schedule()
        categories = [line.category for line in schedule.lines]
        self.assertEqual(categories, [
            constants.CGT_GAIN_DEFERRED_NON_RESIDENTIAL,
            constants.CGT_GAIN_NON_RESIDENTIAL,
        ])

    def test_the_loss_hits_the_deferred_gain_first(self):
        """The taxpayer-unfavourable order, applied because s102-5 Step 1(a) requires it."""
        schedule = self._schedule()
        deferred, non_residential = schedule.lines
        self.assertEqual(
            deferred.current_year_losses_applied.amount, Decimal('2000.00'))
        self.assertEqual(
            non_residential.current_year_losses_applied.amount, Decimal('0'))

    def test_spending_the_loss_the_other_way_would_have_been_worth_more(self):
        """Quantifies what the statutory order costs, so it is not mistaken for a bug."""
        schedule = self._schedule()
        as_required = schedule.net_capital_gain.amount

        deferred, non_residential = schedule.lines
        # The same loss taken off the undiscounted gain instead.
        if_chosen = (
            (deferred.gross_gains.amount) * Decimal('0.5')
            + (non_residential.gross_gains.amount - Decimal('2000.00')))
        self.assertGreater(as_required, if_chosen)


class CGTReportTests(TransactionTestCase):
    """The two new reports, and the one that stays frozen."""

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def test_the_event_report_emits_every_field_of_the_fact_table(self):
        df = CGTEventReport(account=self.account).generate()
        self.assertEqual(list(df.columns), cgt.event_fields())
        self.assertEqual(len(df), 3)

    def test_the_realised_gain_report_did_not_grow_a_column(self):
        """It is what users have been exporting for years, so its shape is a contract."""
        df = RealisedCapitalGainReport(account=self.account).generate()
        self.assertEqual(list(df.columns), [
            'sell_date', 'instrument', 'quantity_sold', 'buy_id', 'parcel_id', 'sell_id',
            'sell_allocation_id', 'buy_date', 'days_held', 'proceeds', 'cost_base',
            'capital_gain', 'fiscal_year',
        ])

    def test_the_schedule_report_refuses_to_call_itself_final(self):
        report = CGTScheduleReport(account=self.account, fiscal_year='FY2023/24')
        self.assertTrue(report.is_draft)
        self.assertTrue(report.warnings())
        self.assertTrue(report.summary()['is_draft'])

    def test_the_schedule_report_carries_the_figures_a_return_asks_for(self):
        summary = CGTScheduleReport(
            account=self.account, fiscal_year='FY2023/24').summary()
        self.assertEqual(
            summary['total_current_year_capital_gains'].amount, Decimal('4401.3617'))
        self.assertEqual(summary['net_capital_gain'].amount, Decimal('2200.68085'))
        self.assertEqual(
            summary['minimum_tax_capital_gain_base'].amount, Decimal('2200.68085'))


class LoadCPICommandTests(TestCase):
    """Loading CPI, including the convention trap in how the ABS dates a quarter."""

    def _write(self, rows):
        handle = tempfile.NamedTemporaryFile(
            mode='w', suffix='.csv', delete=False, newline='', encoding='utf-8')
        handle.write(rows)
        handle.close()
        self.addCleanup(lambda: Path(handle.name).unlink(missing_ok=True))
        return handle.name

    def test_rows_are_loaded_and_normalised_to_the_quarter_start(self):
        """The ABS dates a quarter by its last month, which is a quarter off if taken at
        face value. 30 September 2027 is the September quarter, which starts on 1 July."""
        path = self._write('date,index\n2027-09-30,100.0\n2027-12-31,102.5\n')
        call_command('load_cpi', path)
        self.assertEqual(
            list(CPIIndex.objects.values_list('quarter_start_date', flat=True)),
            [date(2027, 7, 1), date(2027, 10, 1)])

    def test_loading_twice_updates_rather_than_duplicating(self):
        path = self._write('2027-07-01,100.0\n')
        call_command('load_cpi', path)
        call_command('load_cpi', self._write('2027-07-01,101.0\n'))
        self.assertEqual(CPIIndex.objects.count(), 1)
        self.assertEqual(
            CPIIndex.objects.first().index_number, Decimal('101.0000'))

    def test_a_dry_run_writes_nothing(self):
        path = self._write('2027-07-01,100.0\n')
        call_command('load_cpi', path, '--dry-run')
        self.assertEqual(CPIIndex.objects.count(), 0)

    def test_a_file_with_no_usable_rows_is_an_error(self):
        path = self._write('nothing,useful\n')
        with self.assertRaises(CommandError):
            call_command('load_cpi', path)

    def test_a_quarter_must_start_a_quarter(self):
        entry = CPIIndex(quarter_start_date=date(2027, 8, 1), index_number=Decimal('100'))
        with self.assertRaises(ValidationError):
            entry.full_clean()


class CaptureCutoverValuationsCommandTests(TransactionTestCase):
    """Someone has to record the 30 June 2027 close, and it will not happen by itself."""

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account, name='CUT')
        Buy.objects.create(
            account=self.account, instrument=self.instrument, date=date(2020, 1, 15),
            quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(Decimal('19.95'), 'AUD'),
        )
        InstrumentPriceHistory.objects.create(
            account=self.account, instrument=self.instrument, date=date(2027, 6, 30),
            open=Decimal('14'), high=Decimal('16'), low=Decimal('14'),
            close=Decimal('15.5'), volume=1000, stock_splits=Decimal('0'),
        )

    def test_it_records_the_close_for_the_day_before_the_cutover(self):
        """The deemed sale happens just before 1 July, so 30 June is the day to value."""
        call_command('capture_cutover_valuations', '--account', self.account.description)
        valuation = InstrumentValuation.objects.get()
        self.assertEqual(valuation.valuation_date, date(2027, 6, 30))
        self.assertEqual(valuation.unit_value, Money(Decimal('15.500000'), 'AUD'))
        self.assertEqual(valuation.source, 'PRICE_HISTORY')

    def test_it_does_not_overwrite_a_value_the_user_sourced(self):
        InstrumentValuation.objects.create(
            account=self.account, instrument=self.instrument,
            valuation_date=date(2027, 6, 30),
            unit_value=Money(Decimal('14.00'), 'AUD'),
            purpose='CUTOVER_2027', source='USER',
        )
        call_command('capture_cutover_valuations', '--account', self.account.description)
        self.assertEqual(
            InstrumentValuation.objects.get().unit_value, Money(Decimal('14.00'), 'AUD'))

    def test_overwrite_replaces_it(self):
        InstrumentValuation.objects.create(
            account=self.account, instrument=self.instrument,
            valuation_date=date(2027, 6, 30),
            unit_value=Money(Decimal('14.00'), 'AUD'),
            purpose='CUTOVER_2027', source='USER',
        )
        call_command(
            'capture_cutover_valuations', '--account', self.account.description,
            '--overwrite')
        self.assertEqual(
            InstrumentValuation.objects.get().unit_value,
            Money(Decimal('15.500000'), 'AUD'))

    def test_a_dry_run_writes_nothing(self):
        call_command(
            'capture_cutover_valuations', '--account', self.account.description,
            '--dry-run')
        self.assertEqual(InstrumentValuation.objects.count(), 0)

    def test_an_unknown_portfolio_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command('capture_cutover_valuations', '--account', 'no such portfolio')


class IndexationNeverDeepensALossTests(TransactionTestCase):
    """s110-55: a capital loss is worked out on the reduced cost base, which excludes indexation.

    Left unguarded, indexation manufactures a deductible loss out of an asset that merely
    failed to keep pace with inflation. It also produces a third outcome that a gain-or-loss
    model has no room for: where the proceeds fall between the plain and the indexed cost
    base, there is no gain *and* no loss.

    This was found by the loss ordering test reporting a loss of 2,450 on a holding that
    fell 2,000, which is the shape this kind of bug takes -- not an obviously wrong figure,
    just a slightly larger one, in the taxpayer's favour.
    """

    def setUp(self):
        self.account = create_account(
            owner=create_user(username='indexloss'),
            description='Indexation Loss',
            fy_type=create_fiscal_year_type(description='AU Tax Year IL'),
        )
        self.market = create_market(account=self.account)
        declare(self.account, 'RESIDENT', date(2010, 1, 1))
        load_test_cpi()
        enable_2027_regime(self)

    def _sell_at(self, name, unit_price):
        """Bought after the cutover for 10,000, sold in a quarter where CPI is up 4.5%.

        The indexed cost base is therefore 10,450 and the plain one 10,000.
        """
        instrument = create_instrument(
            account=self.account, market=self.market, name=name)
        Buy.objects.create(
            account=self.account, instrument=instrument, date=date(2028, 9, 1),
            quantity=Decimal('1000'), unit_price=Money(Decimal('10.00'), 'AUD'),
            total_brokerage=Money(Decimal('0'), 'AUD'),
        )
        Sell.objects.create(
            account=self.account, instrument=instrument, date=date(2029, 1, 15),
            quantity=Decimal('1000'), unit_price=Money(Decimal(unit_price), 'AUD'),
            total_brokerage=Money(Decimal('0'), 'AUD'), strategy='FIFO',
        )
        return [e for e in cgt.disposal_events(self.account)
                if e.instrument == name][0]

    def test_the_indexation_factor_is_what_this_rests_on(self):
        self.assertEqual(
            cgt.indexation_factor(date(2028, 9, 1), date(2029, 1, 15)),
            Decimal('1.045'))

    def test_a_loss_is_measured_against_the_unindexed_cost_base(self):
        event = self._sell_at('DOWN', '8.00')
        self.assertEqual(event.capital_gain, Money(Decimal('-2000.00'), 'AUD'))
        self.assertEqual(event.cost_base, Money(Decimal('10000.00'), 'AUD'))
        # Indexing it would have reported 2,450: a 450 loss the holder never made.
        self.assertEqual(event.gross_loss, Money(Decimal('2000.00'), 'AUD'))

    def test_proceeds_between_the_two_cost_bases_are_neither_a_gain_nor_a_loss(self):
        """The outcome a two-way model has nowhere to put.

        Up 2% against 4.5% inflation: a real loss, but not a deductible one, and certainly
        not a gain.
        """
        event = self._sell_at('FLAT', '10.20')
        self.assertEqual(event.capital_gain, Money(Decimal('0'), 'AUD'))
        self.assertEqual(event.gross_gain, Money(Decimal('0'), 'AUD'))
        self.assertEqual(event.gross_loss, Money(Decimal('0'), 'AUD'))

    def test_a_gain_is_measured_against_the_indexed_cost_base(self):
        event = self._sell_at('UP', '11.00')
        self.assertEqual(event.cost_base, Money(Decimal('10450.000'), 'AUD'))
        self.assertEqual(event.capital_gain, Money(Decimal('550.000'), 'AUD'))
        self.assertEqual(event.discount_percentage, Decimal('0'))


class ExportRoundTripTests(TransactionTestCase):
    """An export has to be loadable back into the portfolio it came from.

    This is the backup story: `DataExport` is what a user has if their database is lost, and
    it is the only migration path off this application. A sheet that exports but will not
    import is a backup that silently is not one, and the failure only shows up on the day it
    matters.
    """

    def _export(self, account):
        export = DataExport.objects.create(account=account)
        export.refresh_from_db()
        self.assertTrue(export.file, 'the export produced no file')
        return Path(export.file.path)

    def _detached_export(self, account):
        """An export copied out of the media folder, the way a backup is kept.

        Deleting the DataExport row takes its file with it, so an export left where the
        application put it disappears along with the database it was meant to survive. A
        backup that only exists inside the thing being backed up is not one, which is worth
        the test making explicit rather than working around.
        """
        source = self._export(account)
        destination = Path(tempfile.mkdtemp()) / source.name
        shutil.copy2(source, destination)
        return destination

    def _wipe(self):
        """Empty every table, as losing the database would.

        Deleted in the reverse of the order the loader fills them, which is the only
        ordering that is guaranteed to respect the protected foreign keys -- and it stays
        right as models are added, since it is the same list the loader maintains.
        """
        for model in reversed(list(loading.DataLoader.get_model_load_order())):
            model.objects.all().delete()
        DataExport.objects.all().delete()
        AppUser.objects.all().delete()
        self.assertEqual(Account.objects.count(), 0)

    def test_an_export_restores_into_an_empty_database(self):
        """The case the backup exists for, and the one that could not be done at all.

        Restoring is not re-importing. There is no portfolio to load into, and creating one
        first is what broke it: the new user and account get new ids, then the file arrives
        carrying the originals, the user collides on username, and every row naming the old
        account is refused as belonging elsewhere. The file has to supply the portfolio.
        """
        data = create_golden_master_portfolio()
        account = data['account']
        declare(account, 'RESIDENT', date(2000, 1, 1))
        account_id, username = account.id, account.owner.username
        path = self._detached_export(account)

        before = {model.__name__: model.objects.count()
                  for model in (Buy, Sell, Parcel, SellAllocation, Instrument, Market,
                                ResidencyPeriod, AppUser, Account)}

        # Everything goes, exactly as it would in the disaster this is for. The export file
        # lives outside the tables, which is the whole point of it.
        self._wipe()

        loader = loading.DataLoader(input_file=path)

        self.assertEqual(loader.account.id, account_id,
                         'the portfolio must keep the id every other row refers to')
        self.assertEqual(AppUser.objects.get().username, username)
        for name, count in before.items():
            self.assertEqual(
                apps.get_model('share_dinkum_app', name).objects.count(), count,
                f'{name} did not come back with the same number of rows')

    def test_restoring_does_not_derive_what_the_file_already_holds(self):
        """`_creation_handled` has to survive the trip, or the signals derive a second set.

        A buy creates a parcel by signal. On a restore the file already carries the parcels,
        including the ones bifurcated by a partial sale, which no signal could reconstruct.
        Dropping the flag meant both appeared: one set from the file and one conjured, with
        the cost base adjustments then spread across twice as many parcels as exist.
        """
        data = create_golden_master_portfolio()
        account = data['account']
        declare(account, 'RESIDENT', date(2000, 1, 1))
        path = self._detached_export(account)
        parcels_before = Parcel.objects.count()
        self.assertGreater(parcels_before, 0)

        self._wipe()

        loading.DataLoader(input_file=path)

        self.assertEqual(Parcel.objects.count(), parcels_before)
        self.assertEqual(
            Parcel.objects.filter(buy__isnull=False).count(), parcels_before,
            'every parcel should be one the file supplied, not one a signal invented')

    def test_a_column_the_model_no_longer_has_is_ignored(self):
        """Renaming a field must not retire every export taken before it.

        The loader looked up each column and raised on one it could not find, so a single
        renamed field turned every older export into a file that would not load -- and an
        export is the backup.
        """
        account = create_account()
        instrument = create_instrument(account=account)
        df = pd.DataFrame([{
            'id': instrument.id,
            'name': instrument.name,
            'is_taxable_australian_property': True,   # renamed away
            'a_field_that_never_existed': 'x',
        }])

        loader = loading.DataLoader(account=account)
        loader.load_table_to_model(model=Instrument, df=df)

        instrument.refresh_from_db()
        self.assertEqual(instrument.name, instrument.name)

    def test_a_blank_file_cell_loads_as_no_file(self):
        """A blank in a file column is NaN, and NaN is truthy.

        It walked past the `if not value` guard, reached the model, and FileField.pre_save
        asked a float for its `.name`. Nothing in that error mentions a spreadsheet.
        """
        account = create_account()
        instrument = create_instrument(account=account)
        df = pd.DataFrame([{
            'legacy_id': 'B-NAN',
            'instrument__name': instrument.name,
            'date': date(2024, 1, 10),
            'quantity': Decimal('10'),
            'unit_price': Decimal('50'),
            'total_brokerage': Decimal('10'),
            'file': float('nan'),
        }])

        loader = loading.DataLoader(account=account)
        loader.load_table_to_model(model=Buy, df=df)

        buy = Buy.objects.get(legacy_id='B-NAN')
        self.assertFalse(buy.file)

    def test_an_export_reloads_into_its_own_portfolio(self):
        data = create_golden_master_portfolio()
        account = data['account']
        declare(account, 'RESIDENT', date(2000, 1, 1))

        path = self._export(account)
        loading.DataLoader(account=account, input_file=path)

        # Nothing duplicated: every row matched the one it came from.
        self.assertEqual(Buy.objects.filter(account=account).count(), 3)
        self.assertEqual(Sell.objects.filter(account=account).count(), 2)
        self.assertEqual(ResidencyPeriod.objects.filter(account=account).count(), 1)
        self.assertEqual(AppUser.objects.count(), 1)

    def test_a_blank_text_column_loads_as_empty_rather_than_failing(self):
        """The specific shape of the bug, isolated from the rest of the round trip.

        An exported AppUser has an empty email, because most users never set one. Excel has
        no way to distinguish an empty string from an absent value, so it comes back as a
        blank cell, and the loader turned every blank into None. `email` is NOT NULL with a
        default of empty string, as Django's own `blank=True, null=False` idiom requires, so
        the insert failed on a constraint.
        """
        account = create_account()
        generator = excelinterface.ExcelGen(title='Blank email')
        generator.add_table(
            pd.DataFrame([{
                'id': str(account.owner.id),
                'username': account.owner.username,
                'email': None,
                'first_name': None,
                'is_active': True,
            }]),
            table_name='AppUser',
        )
        path = Path(tempfile.mkdtemp()) / 'blank.xlsx'
        generator.save(path)

        loading.DataLoader(account=account, input_file=path)

        account.owner.refresh_from_db()
        self.assertEqual(account.owner.email, '')
        self.assertEqual(account.owner.first_name, '')

    def test_a_genuinely_missing_required_value_still_fails(self):
        """The fix must not turn every blank into a default and swallow real errors.

        Only text columns that are NOT NULL get an empty string, because that is what Django
        means by `blank=True, null=False`. A missing date or quantity is a broken row and has
        to say so.
        """
        account = create_account()
        market = create_market(account=account)
        create_instrument(account=account, market=market, name='BHP')

        generator = excelinterface.ExcelGen(title='Missing date')
        generator.add_table(
            pd.DataFrame([{
                'legacy_id': 'broken-1',
                'instrument__name': 'BHP',
                'date': None,
                'quantity': Decimal('100'),
                'unit_price': Decimal('40'),
                'unit_price_currency': 'AUD',
                'total_brokerage': Decimal('10'),
                'total_brokerage_currency': 'AUD',
            }]),
            table_name='Buy',
        )
        path = Path(tempfile.mkdtemp()) / 'broken.xlsx'
        generator.save(path)

        with self.assertRaises(Exception):
            loading.DataLoader(account=account, input_file=path)
        self.assertEqual(Buy.objects.filter(account=account).count(), 0)


class UpgradeIsInertUntilDataIsTouchedTests(TransactionTestCase):
    """Reading a report must never rewrite a stored figure.

    This is what makes the upgrade path safe to describe. Every migration in this release is
    additive -- CreateModel and AddField, no data migration -- so applying them cannot move a
    number. The corrections to how cost base adjustments are weighted only take effect when
    an adjustment is saved again, which happens on a re-import and not before.

    That means a user can upgrade, look at everything, and still be seeing exactly the
    figures they lodged. It stops being true the moment they re-import, which is precisely
    when the changelog tells them to take a snapshot first. If a report ever starts writing
    on read, that advice becomes wrong and this test is what catches it.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def _allocation_state(self):
        return sorted(
            (str(a.id), a.cost_base_increase.amount)
            for a in CostBaseAdjustmentAllocation.objects.filter(account=self.account)
        )

    def test_generating_every_report_leaves_the_allocations_untouched(self):
        before = self._allocation_state()
        self.assertTrue(before)

        RealisedCapitalGainReport(account=self.account).generate()
        OpenParcelReport(account=self.account).generate()
        CGTEventReport(account=self.account).generate()
        CGTScheduleReport(account=self.account, fiscal_year='FY2023/24').generate()
        cgt.all_events(self.account)

        self.assertEqual(self._allocation_state(), before)

    def test_declaring_residency_does_not_rewrite_anything_either(self):
        before = self._allocation_state()
        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        self.account.taxpayer_type = 'INDIVIDUAL'
        self.account.save()

        CGTEventReport(account=self.account).generate()
        self.assertEqual(self._allocation_state(), before)

    def test_re_saving_an_adjustment_does_not_re_spread_it(self):
        """The boundary, stated explicitly, because it is easy to assume the opposite.

        An adjustment is allocated once, on creation. Saving it again does nothing, and
        neither does re-importing the file it came from, since a re-import updates rather
        than creates. That is what makes an upgrade inert -- and it is also why a correction
        to the weighting needs a command of its own to reach data that already exists.
        """
        adjustment = self.data['adjustment']
        CostBaseAdjustmentAllocation.objects.filter(
            account=self.account, cost_base_adjustment=adjustment).delete()

        adjustment.save()

        self.assertEqual(self._allocation_state(), [])


class FreshImportPicksUpTheCorrectedWeightingTests(ImportWorkbookMixin, TransactionTestCase):
    """The upgrade path for figures recorded before the weighting was corrected.

    A cost base adjustment is spread across parcels once, when it is created, and never
    again. So upgrading does not re-spread the adjustments already in a database, and neither
    does re-importing a file into the portfolio it came from -- a re-import matches and
    updates existing rows rather than creating them.

    Loading the original file into a **new, empty** portfolio does create them, with the
    corrected code, in the right order. That is the supported way to see what the correction
    does to a history, and it is non-destructive: the original portfolio is untouched and the
    two can be compared side by side.

    A command that re-spread adjustments in place was written and then removed. Its output
    depended on when the adjustment was entered relative to the sells, because the allocation
    weights parcels by the days they were held during the year and a parcel's sale date is
    only known once the sell exists. On an imported file the sells are always loaded first, so
    the answer is stable; entered by hand in another order it is not. Rewriting cost bases on
    lodged tax data with an order-dependent result is not a trade worth making.
    """

    def _workbook(self, path):
        """A year with a parcel held throughout and one bought two months before the end.

        The whole point of the correction: the late parcel used to take the same share per
        unit as the one held all year.
        """
        generator = excelinterface.ExcelGen(title='Weighting')
        generator.add_table(
            pd.DataFrame([{'code': 'ASX', 'suffix': 'AX'}]), table_name='Market')
        generator.add_table(
            pd.DataFrame([{'name': 'AMT', 'currency': 'AUD', 'market__code': 'ASX'}]),
            table_name='Instrument')
        generator.add_table(
            pd.DataFrame([
                {'legacy_id': 'all-year', 'instrument__name': 'AMT',
                 'date': date(2023, 7, 1), 'quantity': Decimal('1000'),
                 'unit_price': Decimal('10'), 'unit_price_currency': 'AUD',
                 'total_brokerage': Decimal('0'), 'total_brokerage_currency': 'AUD'},
                {'legacy_id': 'late', 'instrument__name': 'AMT',
                 'date': date(2024, 5, 1), 'quantity': Decimal('1000'),
                 'unit_price': Decimal('10'), 'unit_price_currency': 'AUD',
                 'total_brokerage': Decimal('0'), 'total_brokerage_currency': 'AUD'},
            ]),
            table_name='Buy')
        generator.add_table(
            pd.DataFrame([{
                'legacy_id': 'amit-2024', 'instrument__name': 'AMT',
                'financial_year_end_date': date(2024, 6, 30),
                'cost_base_increase': Decimal('100'),
                'cost_base_increase_currency': 'AUD',
                'allocation_method': 'QTY_HELD',
            }]),
            table_name='CostBaseAdjustment')
        generator.save(path)
        return path

    def test_a_fresh_import_weights_by_the_days_actually_held(self):
        account = create_account()
        path = self._workbook(Path(tempfile.mkdtemp()) / 'weighting.xlsx')
        loading.DataLoader(account=account, input_file=path)

        by_legacy = {
            p.buy.legacy_id: p.total_adjustments.amount
            for p in Parcel.objects.filter(account=account)
            if p.remaining_quantity or p.sale_date
        }
        self.assertEqual(len(by_legacy), 2)

        # 366 days against 61, on equal quantities, so roughly six to one -- not one to one,
        # which is what the old weighting gave.
        self.assertGreater(by_legacy['all-year'], by_legacy['late'] * 5)
        self.assertEqual(sum(by_legacy.values()), Decimal('100.00'))

    def test_reloading_the_same_file_does_not_re_spread_it(self):
        """Which is why a fresh portfolio, not a re-import, is the path that picks it up."""
        account = create_account()
        path = self._workbook(Path(tempfile.mkdtemp()) / 'weighting.xlsx')
        loading.DataLoader(account=account, input_file=path)

        before = sorted(
            a.cost_base_increase.amount
            for a in CostBaseAdjustmentAllocation.objects.filter(account=account))

        loading.DataLoader(account=account, input_file=path)

        after = sorted(
            a.cost_base_increase.amount
            for a in CostBaseAdjustmentAllocation.objects.filter(account=account))
        self.assertEqual(after, before)
        self.assertEqual(CostBaseAdjustment.objects.filter(account=account).count(), 1)


class RefreshPricesButtonTests(TransactionTestCase):
    """The dashboard button that replaces ticking a checkbox on the account.

    Refreshing prices used to mean opening the account, ticking a field called "update price
    history", and saving, at which point a signal did the work and unticked it again. Nobody
    found it. The button sets the same flag and saves, so there is still one implementation
    of "refresh this portfolio".
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.user = self.account.owner
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.url = reverse('admin:dashboard_refresh_prices')

    def test_the_button_is_on_the_dashboard(self):
        response = self.client.get(reverse('admin:dashboard'))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('Refresh prices', body)
        self.assertIn(self.url, body)
        # A state changing action has to be a form, not a link.
        self.assertIn('csrfmiddlewaretoken', body)

    def test_it_triggers_the_same_refresh_the_account_field_did(self):
        with patch.object(
                Account, 'update_all_price_history') as prices, patch.object(
                Account, 'update_all_exchange_rate_history') as rates:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], reverse('admin:dashboard'))
        prices.assert_called_once()
        rates.assert_called_once()

    def test_exchange_rates_are_refreshed_before_prices(self):
        """Order matters, and the signal already knew that.

        Saving an instrument stores its value converted at whatever the rate is then, and
        nothing re-converts it afterwards. Refreshing the rate second leaves every holding
        valued at the previous rate. Pinned here because the button is now the way most
        people will reach this code.
        """
        calls = []
        with patch.object(Account, 'update_all_price_history',
                          side_effect=lambda: calls.append('prices')), \
             patch.object(Account, 'update_all_exchange_rate_history',
                          side_effect=lambda: calls.append('rates')):
            self.client.post(self.url)

        self.assertEqual(calls, ['rates', 'prices'])

    def test_the_flag_is_left_clear_afterwards(self):
        """Otherwise every later save of the account would refresh again."""
        with patch.object(Account, 'update_all_price_history'), \
             patch.object(Account, 'update_all_exchange_rate_history'):
            self.client.post(self.url)

        self.account.refresh_from_db()
        self.assertFalse(self.account.update_price_history)

    def test_a_get_does_not_refresh_anything(self):
        """It reaches an external provider and writes, so a prefetch must not set it off."""
        with patch.object(Account, 'update_all_price_history') as prices:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        prices.assert_not_called()

    def test_a_user_who_is_not_staff_is_refused(self):
        """The endpoint is gated exactly like the rest of the admin, and no more.

        There is no anonymous case to test on a local install: `AutoLoginMiddleware` signs
        every request in, deliberately, because the app runs on your own machine against your
        own database. So the meaningful gate is the one that still applies once someone is
        signed in as a user without admin rights.
        """
        outsider = AppUser.objects.create_user(
            username='outsider', password='x', is_staff=False)
        self.client.force_login(outsider)

        with patch.object(Account, 'update_all_price_history') as prices:
            response = self.client.post(self.url)

        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        prices.assert_not_called()

    def test_a_provider_failure_says_so_and_changes_nothing(self):
        with patch.object(Account, 'update_all_price_history',
                          side_effect=RuntimeError('yfinance is down')), \
             patch.object(Account, 'update_all_exchange_rate_history'):
            response = self.client.post(self.url, follow=True)

        body = response.content.decode()
        self.assertIn('Could not refresh prices', body)
        self.assertIn('yfinance is down', body)
        self.assertIn('existing figures are unchanged', body.replace('Your e', 'e'))

    def test_the_page_says_how_current_the_prices_are(self):
        InstrumentPriceHistory.objects.create(
            account=self.account, instrument=self.data['instrument'],
            date=date(2026, 8, 28), open=Decimal('5'), high=Decimal('6'),
            low=Decimal('5'), close=Decimal('5.5'), volume=100,
            stock_splits=Decimal('0'),
        )
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'Latest close held')
        self.assertContains(response, 'Aug. 28, 2026')

    def test_a_portfolio_with_no_prices_says_that_instead(self):
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'No prices held yet')


class CaptureCGTSnapshotCommandTests(TransactionTestCase):
    """The only way to create a snapshot, and therefore the only way the diff report works.

    `capture()` existed but nothing outside the tests called it, so the advice to take a
    snapshot before lodging was not something a user could act on. The payload is
    deliberately not editable in the admin -- a snapshot you can adjust afterwards is not
    evidence of anything -- which left no route to one at all.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']

    def test_it_captures_every_year_that_has_a_sale(self):
        call_command('capture_cgt_snapshot', '--account', self.account.description)

        years = sorted(
            s.fiscal_year.name
            for s in CGTReturnSnapshot.objects.filter(account=self.account))
        self.assertEqual(years, ['FY2023/24', 'FY2024/25'])

    def test_the_figures_match_the_report(self):
        call_command(
            'capture_cgt_snapshot', '--account', self.account.description,
            '--fiscal-year', 'FY2023/24')

        snapshot = CGTReturnSnapshot.objects.get(account=self.account)
        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(
            Decimal(snapshot.totals['total_capital_gain']), Decimal('4401.3617'))

    def test_it_records_which_basis_produced_the_figures(self):
        """Without this a snapshot cannot be compared to anything meaningfully."""
        call_command('capture_cgt_snapshot', '--account', self.account.description)
        self.assertEqual(
            CGTReturnSnapshot.objects.first().basis, cgt.BASIS_LEGACY)

        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        call_command('capture_cgt_snapshot', '--account', self.account.description)
        self.assertEqual(
            CGTReturnSnapshot.objects.filter(basis=cgt.BASIS_DIVISION_115).count(), 2)

    def test_lodged_marks_the_figures_that_were_filed(self):
        call_command(
            'capture_cgt_snapshot', '--account', self.account.description,
            '--fiscal-year', 'FY2023/24', '--lodged')
        self.assertTrue(CGTReturnSnapshot.objects.get(account=self.account).is_lodged)

    def test_a_dry_run_writes_nothing(self):
        call_command(
            'capture_cgt_snapshot', '--account', self.account.description, '--dry-run')
        self.assertEqual(CGTReturnSnapshot.objects.count(), 0)

    def test_an_unknown_fiscal_year_is_an_error(self):
        with self.assertRaises(CommandError):
            call_command(
                'capture_cgt_snapshot', '--account', self.account.description,
                '--fiscal-year', 'FY1999/00')

    def test_the_snapshot_then_feeds_the_basis_change_report(self):
        """The whole point: a snapshot is only useful because something compares against it."""
        call_command(
            'capture_cgt_snapshot', '--account', self.account.description,
            '--fiscal-year', 'FY2023/24')
        df = CGTBasisChangeReport(
            account=self.account,
            fiscal_year=FiscalYear.objects.get(name='FY2023/24')).generate()
        # Nothing has changed since the capture, so every line reconciles.
        self.assertTrue((df['status'] == 'UNCHANGED').all())


class CaptureSnapshotButtonTests(TransactionTestCase):
    """Recording the figures from the dashboard, rather than from a terminal.

    The figures a snapshot holds come from the report and cannot be typed in, and the model
    refuses to let them be edited afterwards. That left the admin with a form nobody could
    usefully fill in, and the only working route was a management command.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.user = self.account.owner
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.url = reverse('admin:dashboard_capture_snapshot')

    def test_the_button_is_on_the_dashboard(self):
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'Record capital gains figures')
        self.assertContains(response, self.url)
        self.assertContains(response, 'Figures have never been recorded')

    def test_it_records_every_year_that_has_a_sale(self):
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)

        years = sorted(
            s.fiscal_year.name
            for s in CGTReturnSnapshot.objects.filter(account=self.account))
        self.assertEqual(years, ['FY2023/24', 'FY2024/25'])

    def test_the_figures_come_from_the_report(self):
        self.client.post(self.url)
        snapshot = CGTReturnSnapshot.objects.get(fiscal_year__name='FY2023/24')
        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(
            Decimal(snapshot.totals['total_capital_gain']), Decimal('4401.3617'))

    def test_nothing_is_marked_as_lodged(self):
        """Whether figures were filed is a claim about the outside world.

        The application has no way to verify it, so it stays a box the user ticks.
        """
        self.client.post(self.url)
        self.assertFalse(
            CGTReturnSnapshot.objects.filter(account=self.account, is_lodged=True).exists())

    def test_the_basis_is_recorded_with_the_figures(self):
        self.client.post(self.url)
        self.assertTrue(
            CGTReturnSnapshot.objects.filter(basis=cgt.BASIS_LEGACY).exists())

        declare(self.account, 'RESIDENT', date(2000, 1, 1))
        self.client.post(self.url)
        self.assertTrue(
            CGTReturnSnapshot.objects.filter(basis=cgt.BASIS_DIVISION_115).exists())

    def test_it_says_when_the_figures_rest_on_an_assumption(self):
        response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'residency has not been declared')

    def test_the_dashboard_then_says_when_they_were_recorded(self):
        self.client.post(self.url)
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'Figures last recorded')

    def test_a_portfolio_with_no_sales_says_so_rather_than_recording_nothing(self):
        empty = create_account(
            owner=create_user(username='nosales'),
            description='Empty',
            fy_type=create_fiscal_year_type(description='AU Tax Year Empty'))
        empty.owner.is_staff = True
        empty.owner.is_superuser = True
        empty.owner.save()
        self.client.force_login(empty.owner)

        response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'no sales yet')
        self.assertEqual(CGTReturnSnapshot.objects.filter(account=empty).count(), 0)

    def test_a_get_records_nothing(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(CGTReturnSnapshot.objects.count(), 0)

    def test_a_user_who_is_not_staff_is_refused(self):
        outsider = AppUser.objects.create_user(
            username='outsider2', password='x', is_staff=False)
        self.client.force_login(outsider)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        self.assertEqual(CGTReturnSnapshot.objects.count(), 0)


class SnapshotColumnPrecisionTests(TransactionTestCase):
    """Precision is now enforced by the columns, not by a serialiser.

    The figures used to go into a JSON blob as text, so how many decimal places they kept
    was whatever `str()` produced -- which for a value reached by division was twenty-eight
    significant digits. Stored in a MoneyField the question does not arise: the column is
    four decimal places and the database enforces it.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.fiscal_year = FiscalYear.objects.get(name='FY2024/25')

    def test_stored_figures_are_money_at_four_places(self):
        snapshot = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fiscal_year)
        for row in snapshot.captured_rows.all():
            for field in ('proceeds', 'cost_base', 'capital_gain'):
                value = getattr(row, field)
                with self.subTest(field=field):
                    self.assertGreaterEqual(value.amount.as_tuple().exponent, -4)

    def test_the_total_is_a_money_amount(self):
        snapshot = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=self.fiscal_year)
        total = snapshot.totals['total_capital_gain']
        self.assertEqual(Decimal(total), Decimal('-1568.6881'))
        self.assertGreaterEqual(Decimal(total).as_tuple().exponent, -4)


class InexactAllocationPrecisionTests(TransactionTestCase):
    """A partial sale whose quantities do not divide evenly.

    Every figure in this application that involves a share of something is reached by a
    division, and Decimal division is inexact: it stops at 28 significant digits and leaves
    the remainder behind. Sell 135 units out of 745 and the proceeds arrive as
    7154.835632530120481927710843 -- twenty-four digits past the cent, none of them money.

    Summing fourteen of those made a portfolio total read 2019.106200000001099999999996
    instead of 2019.1062, which is how this was noticed.
    """

    def setUp(self):
        self.account = create_account(
            owner=create_user(username='inexact'),
            description='Inexact',
            fy_type=create_fiscal_year_type(description='AU Tax Year Inexact'))
        market = create_market(account=self.account)
        instrument = create_instrument(
            account=self.account, market=market, name='VDHG')

        Buy.objects.create(
            account=self.account, instrument=instrument, date=date(2022, 3, 1),
            quantity=Decimal('745'), unit_price=Money(Decimal('53.17'), 'AUD'),
            total_brokerage=Money(Decimal('9.50'), 'AUD'),
        )
        # 135 of 745 is a repeating fraction, which is the whole point of the fixture.
        Sell.objects.create(
            account=self.account, instrument=instrument, date=date(2024, 5, 20),
            quantity=Decimal('135'), unit_price=Money(Decimal('53.00'), 'AUD'),
            total_brokerage=Money(Decimal('9.50'), 'AUD'), strategy='FIFO',
        )

    def test_reported_money_stops_at_four_decimal_places(self):
        for event in cgt.disposal_events(self.account):
            for field in ('net_proceeds', 'cost_base', 'capital_gain',
                          'gross_proceeds', 'buy_consideration'):
                value = getattr(event, field)
                with self.subTest(field=field):
                    self.assertGreaterEqual(
                        value.amount.as_tuple().exponent, -4, f'{field} = {value}')

    def test_proceeds_less_cost_base_equals_the_reported_gain(self):
        """It did not, quite.

        `net_proceeds` divided before multiplying and `capital_gain` multiplied before
        dividing, so the same quantity was reached two ways and the answers parted company
        in the last digit. Rounding hides that, but the formulas are now the same one.
        """
        for event in cgt.disposal_events(self.account):
            self.assertEqual(
                event.net_proceeds - event.cost_base, event.capital_gain)

    def test_a_snapshot_total_is_a_money_amount(self):
        fiscal_year = FiscalYear.objects.get(name='FY2023/24')
        snapshot = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=fiscal_year)

        total = snapshot.totals['total_capital_gain']
        self.assertGreaterEqual(Decimal(total).as_tuple().exponent, -4, total)

    def test_a_figure_carrying_old_precision_compares_as_unchanged(self):
        """The compatibility guarantee for snapshots taken before figures were rounded.

        They hold values like 7154.835632530120481927710843. A fresh calculation now gives
        7154.8356. Comparing raw would report every row of every old snapshot as changed by
        a hundred-thousandth of a cent, burying anything that mattered, so the comparison
        happens at the four places the application stores money to.
        """
        drifted = Decimal('7154.835632530120481927710843')
        rounded = Decimal('7154.8356')
        self.assertNotEqual(drifted, rounded)
        self.assertEqual(
            CGTBasisChangeReport._as_decimal(drifted),
            CGTBasisChangeReport._as_decimal(rounded),
        )

    def test_a_snapshot_taken_now_reports_no_change(self):
        fiscal_year = FiscalYear.objects.get(name='FY2023/24')
        CGTReturnSnapshot.capture(account=self.account, fiscal_year=fiscal_year)
        df = CGTBasisChangeReport(
            account=self.account, fiscal_year=fiscal_year).generate()
        self.assertTrue((df['status'] == 'UNCHANGED').all())

    def test_a_real_change_is_still_reported(self):
        """The tolerance must not be wide enough to hide something that matters."""
        fiscal_year = FiscalYear.objects.get(name='FY2023/24')
        snapshot = CGTReturnSnapshot.capture(
            account=self.account, fiscal_year=fiscal_year)

        row = snapshot.captured_rows.first()
        row.capital_gain = Money(row.capital_gain.amount + Decimal('0.01'), 'AUD')
        row.save()

        df = CGTBasisChangeReport(
            account=self.account, fiscal_year=fiscal_year).generate()
        self.assertTrue((df['status'] == 'CHANGED').any())


class ExportButtonTests(TransactionTestCase):
    """Exporting from the dashboard, and getting the file back rather than a link to it.

    Exporting meant opening Data exports, adding a record, saving it, and then finding the
    file that the save had generated. The record is worth keeping -- it is the history of
    what was exported and when -- but it should not be the interface.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.user = self.account.owner
        self.user.is_staff = True
        self.user.is_superuser = True
        self.user.save()
        self.client.force_login(self.user)
        self.url = reverse('admin:dashboard_export')

    def _download(self, **post):
        """Post, and drain the response so its file handle is released.

        FileResponse holds the file open until the body is consumed. A real client always
        consumes it; a test that does not leaves the handle open, and on Windows the next
        thing to touch that file fails.
        """
        response = self.client.post(self.url, post or None)
        if hasattr(response, 'streaming_content'):
            response.body = b''.join(response.streaming_content)
        return response

    def test_the_button_is_on_the_dashboard(self):
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'Export portfolio')
        self.assertContains(response, self.url)
        self.assertContains(response, 'Never exported')

    def test_it_returns_the_file_itself(self):
        """A link would depend on media being served, and would hand out a URL to the whole
        portfolio. The response is the file."""
        response = self._download()

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertIn('.xlsx', response['Content-Disposition'])
        body = response.body
        # An xlsx is a zip, so it starts with the zip magic number.
        self.assertTrue(body.startswith(b'PK'), 'not an xlsx file')
        self.assertGreater(len(body), 5000)

    def test_the_export_is_still_recorded(self):
        self._download()
        export = DataExport.objects.get(account=self.account)
        self.assertTrue(export.file)
        self.assertFalse(export.include_price_history)

    def test_price_history_is_left_out_unless_asked_for(self):
        """It is the largest table by far, and unlike everything else it can be fetched
        again from the market."""
        self._download()
        self.assertFalse(DataExport.objects.get(account=self.account).include_price_history)

        DataExport.objects.all().delete()
        self._download(include_price_history='on')
        self.assertTrue(DataExport.objects.get(account=self.account).include_price_history)

    def test_the_file_loads_back_in(self):
        """The claim the button makes is that this is a backup, so it has to be one."""
        response = self._download()

        path = Path(tempfile.mkdtemp()) / 'export.xlsx'
        path.write_bytes(response.body)

        loading.DataLoader(account=self.account, input_file=path)
        self.assertEqual(Buy.objects.filter(account=self.account).count(), 3)
        self.assertEqual(Sell.objects.filter(account=self.account).count(), 2)

    def test_the_dashboard_then_says_when(self):
        self._download()
        response = self.client.get(reverse('admin:dashboard'))
        self.assertContains(response, 'Last exported')

    def test_a_failure_says_so_and_keeps_the_user_on_the_dashboard(self):
        with patch.object(
                excelinterface.ExcelGen, 'save', side_effect=RuntimeError('disk full')):
            response = self.client.post(self.url, follow=True)
        self.assertContains(response, 'Could not build the export')
        self.assertContains(response, 'disk full')

    def test_a_get_exports_nothing(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)
        self.assertEqual(DataExport.objects.count(), 0)

    def test_a_user_who_is_not_staff_is_refused(self):
        outsider = AppUser.objects.create_user(
            username='outsider3', password='x', is_staff=False)
        self.client.force_login(outsider)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])
        self.assertEqual(DataExport.objects.count(), 0)


class ManualAllocationImportTests(TransactionTestCase):
    """Importing a file that pins its own sell allocations, and importing it twice.

    A sell normally allocates itself against parcels by FIFO or lowest gain. A file can
    instead state the allocations, which is what `strategy = MANUAL` and the
    `lookup_legacy_buy` column are for: it is how a history worked out elsewhere is carried
    across without this application re-deriving it and getting different parcels.
    """

    def _workbook(self, path, allocations=None):
        if allocations is None:
            allocations = [{
                'legacy_id': 'A001', 'lookup_legacy_buy': 'B002',
                'lookup_legacy_sell': 'S001', 'quantity': Decimal('60'),
            }]

        generator = excelinterface.ExcelGen(title='Manual allocations')
        generator.add_table(
            pd.DataFrame([{'code': 'ASX', 'suffix': 'AX'}]), table_name='Market')
        generator.add_table(
            pd.DataFrame([{'name': 'BHP', 'currency': 'AUD', 'market__code': 'ASX'}]),
            table_name='Instrument')
        generator.add_table(
            pd.DataFrame([
                {'legacy_id': 'B001', 'instrument__name': 'BHP',
                 'date': date(2022, 7, 1), 'quantity': Decimal('100'),
                 'unit_price': Decimal('10'), 'unit_price_currency': 'AUD',
                 'total_brokerage': Decimal('0'), 'total_brokerage_currency': 'AUD'},
                {'legacy_id': 'B002', 'instrument__name': 'BHP',
                 'date': date(2023, 7, 1), 'quantity': Decimal('100'),
                 'unit_price': Decimal('12'), 'unit_price_currency': 'AUD',
                 'total_brokerage': Decimal('0'), 'total_brokerage_currency': 'AUD'},
            ]),
            table_name='Buy')
        generator.add_table(
            pd.DataFrame([{
                'legacy_id': 'S001', 'instrument__name': 'BHP',
                'date': date(2024, 7, 1), 'quantity': Decimal('100'),
                'unit_price': Decimal('15'), 'unit_price_currency': 'AUD',
                'total_brokerage': Decimal('0'), 'total_brokerage_currency': 'AUD',
                'strategy': 'MANUAL',
            }]),
            table_name='Sell')
        generator.add_table(pd.DataFrame(allocations), table_name='SellAllocation')
        generator.save(path)
        return path

    def setUp(self):
        self.account = create_account()
        self.path = self._workbook(Path(tempfile.mkdtemp()) / 'manual.xlsx')

    def test_a_pinned_allocation_loads_against_the_buy_it_names(self):
        loading.DataLoader(account=self.account, input_file=self.path)

        allocation = SellAllocation.objects.get(account=self.account)
        self.assertEqual(allocation.quantity, Decimal('60'))
        # Against B002, not the older B001 that FIFO would have taken.
        self.assertEqual(allocation.parcel.buy.legacy_id, 'B002')

    def test_loading_the_same_file_twice_updates_rather_than_failing(self):
        """The promise the loader makes everywhere else, which this path did not keep.

        The parcel a pinned allocation names was resolved before checking whether the
        allocation already existed, and the resolution only accepts a parcel with quantity
        still available. On a second load that parcel has already been consumed by the
        allocation from the first, so the lookup found nothing and the import died on its
        first row -- with a bare assertion that said neither which row nor what was wrong.
        """
        # The whole of B002, so nothing of it is left over. A partial sale leaves an
        # unsold remnant and the lookup still finds exactly one parcel, which is why this
        # went unnoticed: it only fails once a holding is completely sold.
        path = self._workbook(
            Path(tempfile.mkdtemp()) / 'full.xlsx',
            allocations=[{
                'legacy_id': 'A001', 'lookup_legacy_buy': 'B002',
                'lookup_legacy_sell': 'S001', 'quantity': Decimal('100'),
            }])
        loading.DataLoader(account=self.account, input_file=path)
        loading.DataLoader(account=self.account, input_file=path)

        self.assertEqual(SellAllocation.objects.filter(account=self.account).count(), 1)
        allocation = SellAllocation.objects.get(account=self.account)
        self.assertEqual(allocation.quantity, Decimal('100'))
        self.assertEqual(allocation.parcel.buy.legacy_id, 'B002')

    def test_an_unknown_buy_says_which_one_and_why(self):
        """A bare AssertionError told the user nothing at all."""
        path = self._workbook(
            Path(tempfile.mkdtemp()) / 'missing.xlsx',
            allocations=[{
                'legacy_id': 'A001', 'lookup_legacy_buy': 'NOPE',
                'lookup_legacy_sell': 'S001', 'quantity': Decimal('60'),
            }])
        with self.assertRaises(Exception) as caught:
            loading.DataLoader(account=self.account, input_file=path)
        message = str(caught.exception)
        self.assertIn('NOPE', message)
        self.assertIn('no buy with that legacy id', message.lower())

    def test_an_over_allocated_buy_says_so(self):
        """Two pinned allocations taking more than the buy ever held."""
        path = self._workbook(
            Path(tempfile.mkdtemp()) / 'over.xlsx',
            allocations=[
                {'legacy_id': 'A001', 'lookup_legacy_buy': 'B002',
                 'lookup_legacy_sell': 'S001', 'quantity': Decimal('100')},
                {'legacy_id': 'A002', 'lookup_legacy_buy': 'B002',
                 'lookup_legacy_sell': 'S001', 'quantity': Decimal('100')},
            ])
        with self.assertRaises(Exception) as caught:
            loading.DataLoader(account=self.account, input_file=path)
        self.assertIn('B002', str(caught.exception))


class LegalFormSourceTests(TransactionTestCase):
    """Who decided the legal form, and why nobody should have to type it.

    `legal_form_source` gates `is_classified`, which gates whether a capital gains schedule
    will call itself final. Nothing ever set it to USER, so the gate could not be passed:
    every instrument stayed unconfirmed and every schedule stayed a draft, forever, no
    matter what the user did in the admin.
    """

    def setUp(self):
        self.account = create_account()
        self.market = create_market(account=self.account)

    def _instrument(self, name='BHP', **kwargs):
        return Instrument.objects.create(
            account=self.account, market=self.market, name=name,
            currency=DEFAULT_CURRENCY, **kwargs)

    def test_an_untouched_instrument_is_not_classified(self):
        instrument = self._instrument()
        self.assertEqual(instrument.legal_form, 'UNKNOWN')
        self.assertFalse(instrument.is_classified)

    def test_setting_the_legal_form_confirms_it(self):
        """What a user does in the admin. They never see the source field."""
        instrument = self._instrument()
        instrument.legal_form = 'COMPANY'
        instrument.save()

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'USER')
        self.assertTrue(instrument.is_classified)

    def test_creating_with_a_legal_form_counts_as_confirmed(self):
        """An import file that states the legal form is stating the user's answer."""
        instrument = self._instrument(name='VAS', legal_form='UNIT_TRUST')
        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'USER')
        self.assertTrue(instrument.is_classified)

    def test_a_suggestion_does_not_count_as_confirmed(self):
        """The suggester says so by setting the source in the same save."""
        instrument = self._instrument()
        instrument.legal_form = 'COMPANY'
        instrument.legal_form_source = 'SUGGESTED'
        instrument.save()

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'SUGGESTED')
        self.assertFalse(instrument.is_classified)

    def test_a_user_correcting_a_suggestion_confirms_it(self):
        """The case that matters: the command guessed, you disagree, you fix it."""
        instrument = self._instrument()
        instrument.legal_form = 'COMPANY'
        instrument.legal_form_source = 'SUGGESTED'
        instrument.save()

        instrument.legal_form = 'UNIT_TRUST'
        instrument.save()

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'USER')
        self.assertTrue(instrument.is_classified)

    def test_agreeing_with_a_suggestion_by_re_saving_does_not_confirm_it(self):
        """Saving an unrelated field must not silently promote a guess to an answer."""
        instrument = self._instrument()
        instrument.legal_form = 'COMPANY'
        instrument.legal_form_source = 'SUGGESTED'
        instrument.save()

        instrument.description = 'BHP Group'
        instrument.save()

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'SUGGESTED')

    def test_the_suggest_command_still_marks_its_own_work_as_suggested(self):
        instrument = self._instrument(name='VAS')
        Distribution.objects.create(
            account=self.account, instrument=instrument, date=date(2024, 1, 15),
            quantity=Decimal('100'),
            distribution_amount_per_share=Money(Decimal('0.50'), 'AUD'),
        )
        call_command(
            'suggest_instrument_classification', '--account', self.account.description)

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form, 'UNIT_TRUST')
        self.assertEqual(instrument.legal_form_source, 'SUGGESTED')
        self.assertFalse(instrument.is_classified)


class InlineFieldBudgetTests(TransactionTestCase):
    """A change page has to be submittable, not just renderable.

    Every form field is a POST parameter, and Django refuses a submission carrying too many.
    The page renders perfectly and then fails on Save, which reads as saving being broken
    rather than the page being too large -- and it blocked confirming an instrument's legal
    form, which is a single checkbox on a page carrying a decade of dividends.
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from share_dinkum_app.admin import GenericModelAdmin

        self.account = create_account()
        self.instrument = create_instrument(account=self.account)
        self.admin = GenericModelAdmin(Instrument, AdminSite())
        for index in range(12):
            Buy.objects.create(
                account=self.account, instrument=self.instrument,
                date=date(2020, 1, 1) + timedelta(days=index),
                quantity=Decimal('10'), unit_price=Money(50, 'AUD'),
                total_brokerage=Money(10, 'AUD'))

    def _request(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = AppUser.objects.first()
        return request

    def _models(self):
        return {i.model.__name__
                for i in self.admin.get_inline_instances(self._request(), self.instrument)}

    def test_inlines_are_shown_within_the_budget(self):
        self.assertIn('Buy', self._models())

    def test_an_inline_over_the_budget_is_dropped_rather_than_breaking_the_save(self):
        """Dropping the inline loses a convenience; exceeding the limit loses the page."""
        self.admin.INLINE_FIELD_BUDGET = 10
        self.assertNotIn('Buy', self._models())

    def test_the_budget_is_measured_in_fields_rather_than_rows(self):
        """Rows are the wrong unit: a wide model costs more per row than a narrow one.

        Set the budget just under what the buys actually cost and the inline must go, even
        though there are only twelve rows -- well inside the 200-row rule that governed this
        before and could not see the difference.
        """
        editable = sum(1 for f in Buy._meta.fields if f.editable)
        self.admin.INLINE_FIELD_BUDGET = editable * 12
        self.assertNotIn('Buy', self._models())

        self.admin.INLINE_FIELD_BUDGET = editable * 13
        self.assertIn('Buy', self._models())


class CostBaseAgreesWithStatementTests(TransactionTestCase):
    """The statement and the adjustment are entered separately, so they can disagree.

    Nothing read the cost base components before this: they were recorded for completeness
    and no code touched them, so a statement could state one figure while the adjustment
    that actually moves parcel cost bases carried another, and the schedule said nothing.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account)
        self.end = date(2024, 6, 30)

    def _pair(self, recorded, components):
        adjustment = CostBaseAdjustment.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=self.end,
            cost_base_increase=Money(Decimal(recorded), 'AUD'))
        statement = AttributionStatement.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=self.end, cost_base_adjustment=adjustment)
        for component, amount in components:
            AttributionComponent.objects.create(
                account=self.account, statement=statement, component=component,
                amount=Money(Decimal(amount), 'AUD'))
        return statement

    def test_a_matching_shortfall_agrees(self):
        statement = self._pair('251.84', [('COSTBASE_INCREASE', '251.84')])
        self.assertEqual(statement.stated_cost_base_movement, Decimal('251.84'))
        self.assertTrue(statement.cost_base_agrees)

    def test_an_excess_is_a_reduction(self):
        statement = self._pair('-86.98', [('COSTBASE_DECREASE', '86.98')])
        self.assertEqual(statement.stated_cost_base_movement, Decimal('-86.98'))
        self.assertTrue(statement.cost_base_agrees)

    def test_equal_legs_net_to_nil_rather_than_disagreeing(self):
        """The case that makes reading one leg alone wrong.

        A statement can declare a large excess and an equal shortfall. Checking against the
        shortfall alone would report a 1,958.03 discrepancy on an adjustment that is
        correctly nil -- which is a real statement from this portfolio, twice.
        """
        statement = self._pair('0.00', [('COSTBASE_INCREASE', '1958.03'),
                                        ('COSTBASE_DECREASE', '1958.03')])
        self.assertEqual(statement.stated_cost_base_movement, Decimal('0'))
        self.assertTrue(statement.cost_base_agrees)

    def test_a_pre_amit_tax_deferred_amount_reduces_the_cost_base(self):
        statement = self._pair('-23.30', [('TAX_DEFERRED', '23.30')])
        self.assertEqual(statement.stated_cost_base_movement, Decimal('-23.30'))
        self.assertTrue(statement.cost_base_agrees)

    def test_a_non_attributable_amount_reduces_the_cost_base(self):
        statement = self._pair('-671.54', [('NON_ATTRIBUTABLE', '671.54')])
        self.assertTrue(statement.cost_base_agrees)

    def test_the_amit_pair_takes_precedence_over_other_lines(self):
        """A statement can state both; the AMIT net amount is the governing figure."""
        statement = self._pair('122.70', [('COSTBASE_INCREASE', '122.70'),
                                          ('NON_ATTRIBUTABLE', '66.53')])
        self.assertEqual(statement.stated_cost_base_movement, Decimal('122.70'))
        self.assertTrue(statement.cost_base_agrees)

    def test_a_disagreement_is_reported(self):
        statement = self._pair('100.00', [('COSTBASE_INCREASE', '251.84')])
        self.assertFalse(statement.cost_base_agrees)

    def test_nothing_to_compare_is_not_a_pass(self):
        """An absent check must not read as a passing one."""
        no_component = self._pair('100.00', [])
        self.assertIsNone(no_component.stated_cost_base_movement)
        self.assertIsNone(no_component.cost_base_agrees)

        unlinked = AttributionStatement.objects.create(
            account=self.account, instrument=create_instrument(
                account=self.account, market=self.instrument.market, name='OTH'),
            financial_year_end_date=self.end)
        self.assertIsNone(unlinked.cost_base_agrees)


class ReverseOneToOneInlineTests(TransactionTestCase):
    """A reverse one-to-one is an object, not a manager, and raises when it is absent.

    `AttributionStatement.cost_base_adjustment` is the app's only `OneToOneField`, and its
    reverse accessor made every CostBaseAdjustment change page a 500 until a statement was
    linked to it -- which is to say all of them, since the page is where you would go to
    link one. A reverse foreign key hands back an empty manager and never raises, so the
    generic loop over `related_objects` had no reason to expect this.
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from share_dinkum_app.admin import GenericModelAdmin

        self.account = create_account()
        self.instrument = create_instrument(account=self.account)
        self.admin = GenericModelAdmin(CostBaseAdjustment, AdminSite())
        self.adjustment = CostBaseAdjustment.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=date(2024, 6, 30),
            cost_base_increase=Money(Decimal('100'), 'AUD'),
        )

    def _request(self):
        from django.test import RequestFactory
        request = RequestFactory().get('/')
        request.user = AppUser.objects.first()
        return request

    def test_the_change_page_builds_without_a_linked_statement(self):
        inlines = self.admin.get_inline_instances(self._request(), self.adjustment)
        models_shown = {inline.model for inline in inlines}
        self.assertIn(AttributionStatement, models_shown)

    def test_the_change_page_builds_with_one(self):
        AttributionStatement.objects.create(
            account=self.account, instrument=self.instrument,
            financial_year_end_date=date(2024, 6, 30),
            cost_base_adjustment=self.adjustment,
        )
        inlines = self.admin.get_inline_instances(self._request(), self.adjustment)
        self.assertIn(AttributionStatement, {inline.model for inline in inlines})


class ConfirmLegalFormInAdminTests(TransactionTestCase):
    """Agreeing with a suggestion, which is the case the model alone cannot express.

    `Instrument.save()` promotes to USER only when the legal form changes, so a user who
    reads a suggestion, decides it is right, and saves records nothing. That is the
    commonest confirmation there is, and without a way to make it every schedule built on
    a suggested instrument stays a draft.
    """

    def setUp(self):
        from django.contrib.admin.sites import AdminSite
        from share_dinkum_app.admin import InstrumentAdmin

        self.account = create_account()
        self.market = create_market(account=self.account)
        self.admin = InstrumentAdmin(Instrument, AdminSite())
        self.user = AppUser.objects.first()

    def _suggested(self, name='VAS', legal_form='UNIT_TRUST'):
        instrument = Instrument.objects.create(
            account=self.account, market=self.market, name=name,
            currency=DEFAULT_CURRENCY)
        instrument.legal_form = legal_form
        instrument.legal_form_source = 'SUGGESTED'
        instrument.save()
        return instrument

    def _request(self):
        """A request the admin can attach messages to."""
        from django.contrib.messages.storage.fallback import FallbackStorage
        from django.test import RequestFactory

        request = RequestFactory().post('/')
        request.user = self.user
        request.session = {}
        request._messages = FallbackStorage(request)
        return request

    def test_the_action_confirms_a_suggestion_without_changing_it(self):
        instrument = self._suggested()
        self.admin.confirm_legal_form_action(
            self._request(), Instrument.objects.filter(pk=instrument.pk))

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form, 'UNIT_TRUST')
        self.assertEqual(instrument.legal_form_source, 'USER')
        self.assertTrue(instrument.is_classified)

    def test_the_action_confirms_in_bulk(self):
        """The back catalogue case: a portfolio's worth of closed positions at once."""
        names = ['VAS', 'VGS', 'STW']
        for name in names:
            self._suggested(name=name)

        self.admin.confirm_legal_form_action(
            self._request(), Instrument.objects.filter(name__in=names))

        for name in names:
            instrument = Instrument.objects.get(name=name)
            self.assertTrue(instrument.is_classified, f'{name} was not confirmed')

    def test_the_action_will_not_confirm_an_instrument_with_no_legal_form(self):
        """There is nothing to confirm, and inventing one would decide a tax outcome."""
        instrument = Instrument.objects.create(
            account=self.account, market=self.market, name='???',
            currency=DEFAULT_CURRENCY)

        self.admin.confirm_legal_form_action(
            self._request(), Instrument.objects.filter(pk=instrument.pk))

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form, 'UNKNOWN')
        self.assertFalse(instrument.is_classified)

    def test_the_tick_confirms_on_the_change_form(self):
        instrument = self._suggested()
        form = self.admin.get_form(self._request(), instrument)(
            instance=instrument,
            data={'account': self.account.pk, 'market': self.market.pk,
                  'name': 'VAS', 'currency': DEFAULT_CURRENCY,
                  'legal_form': 'UNIT_TRUST', 'confirm_legal_form': 'on'},
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.admin.save_model(self._request(), form.instance, form, change=True)

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'USER')

    def test_leaving_the_tick_clear_leaves_the_suggestion_alone(self):
        """Editing an unrelated field must not answer the question on the user's behalf."""
        instrument = self._suggested()
        form = self.admin.get_form(self._request(), instrument)(
            instance=instrument,
            data={'account': self.account.pk, 'market': self.market.pk,
                  'name': 'VAS', 'currency': DEFAULT_CURRENCY,
                  'legal_form': 'UNIT_TRUST', 'description': 'Vanguard Australian Shares'},
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.admin.save_model(self._request(), form.instance, form, change=True)

        instrument.refresh_from_db()
        self.assertEqual(instrument.legal_form_source, 'SUGGESTED')
        self.assertFalse(instrument.is_classified)

    def test_the_tap_override_options_say_what_they_do(self):
        """"Unknown" invites an answer, and "No" is true while still being a trap.

        The dangerous option is the honest one: an ordinary listed share is not taxable
        Australian property in its own right, and saying so overrides the departure deeming.
        The label has to carry that, because nothing else the user sees does.
        """
        instrument = self._suggested()
        form = self.admin.get_form(self._request(), instrument)(instance=instrument)
        rendered = str(form['is_taxable_australian_property_override'])

        self.assertIn('Unset', rendered)
        self.assertNotIn('Unknown', rendered)
        self.assertIn('derive it per parcel', rendered)
        self.assertIn('overriding the departure deeming', rendered)

    def test_the_tap_override_still_round_trips_all_three_states(self):
        """Only the labels changed, so the submitted values must still mean what they did."""
        from share_dinkum_app.admin import UnsetNullBooleanSelect

        widget = UnsetNullBooleanSelect()
        for submitted, expected in [('unknown', None), ('true', True), ('false', False)]:
            self.assertIs(
                widget.value_from_datadict({'f': submitted}, {}, 'f'), expected,
                f'{submitted} did not round-trip')

    def test_the_tick_is_offered_only_where_there_is_something_to_confirm(self):
        suggested = self._suggested()
        self.assertIn('confirm_legal_form', self.admin.get_fields(self._request(), suggested))

        confirmed = self._suggested(name='VGS')
        confirmed.legal_form_source = 'USER'
        confirmed.save()
        self.assertNotIn(
            'confirm_legal_form', self.admin.get_fields(self._request(), confirmed))

        unknown = Instrument.objects.create(
            account=self.account, market=self.market, name='???',
            currency=DEFAULT_CURRENCY)
        self.assertNotIn('confirm_legal_form', self.admin.get_fields(self._request(), unknown))
        self.assertNotIn('confirm_legal_form', self.admin.get_fields(self._request(), None))


class ClassificationClearsTheDraftTests(TransactionTestCase):
    """End to end: confirming the legal form is what lets a schedule call itself final."""

    def test_a_confirmed_instrument_clears_the_draft_warning(self):
        """End to end: this is what the whole field exists to do."""
        data = create_golden_master_portfolio()
        account = data['account']
        account.taxpayer_type = 'INDIVIDUAL'
        account.save()
        declare(account, 'RESIDENT', date(2000, 1, 1))
        data['instrument'].market.country = 'AU'
        data['instrument'].market.save()

        schedule = cgt.build_schedule(account, 'FY2023/24')
        self.assertTrue(any('suggested rather than confirmed' in w or 'not been classified' in w
                            for w in schedule.warnings))

        data['instrument'].legal_form = 'COMPANY'
        data['instrument'].save()

        schedule = cgt.build_schedule(account, 'FY2023/24')
        self.assertEqual(schedule.warnings, [])
        self.assertFalse(schedule.is_draft)


class AssetCategoryOverrideTests(TransactionTestCase):
    """The override names one of eight boxes on a form, not an arbitrary label.

    It was free text, and `asset_category()` returned whatever was in it. Anything typed
    there went onto a capital gains schedule as a category, which is exactly what having a
    fixed vocabulary is supposed to prevent.
    """

    def setUp(self):
        self.account = create_account()
        self.market = create_market(account=self.account)
        self.market.country = 'AU'
        self.market.save()
        self.instrument = create_instrument(
            account=self.account, market=self.market, name='BHP')
        self.instrument.legal_form = 'COMPANY'
        self.instrument.save()

    def test_the_field_offers_only_the_real_categories(self):
        field = Instrument._meta.get_field('cgt_asset_category_override')
        # The eight real boxes. UNCLASSIFIED is an answer the application reaches on its
        # own when it does not know, not something to be selected as an override.
        self.assertEqual(
            [value for value, _label in field.choices],
            [c.value for c in CGTAssetCategory.reportable()])
        self.assertEqual(len(field.choices), 8)
        self.assertNotIn(CGTAssetCategory.UNCLASSIFIED, dict(field.choices))

    def test_without_an_override_the_category_is_derived(self):
        self.assertEqual(
            cgt.asset_category(self.instrument),
            CGTAssetCategory.AU_LISTED_SHARES)

    def test_a_valid_override_wins(self):
        self.instrument.cgt_asset_category_override = CGTAssetCategory.OTHER_ASSETS
        self.assertEqual(
            cgt.asset_category(self.instrument), CGTAssetCategory.OTHER_ASSETS)

    def test_a_form_refuses_anything_else(self):
        self.instrument.cgt_asset_category_override = 'Shares in Aus listed companys'
        with self.assertRaises(ValidationError):
            self.instrument.full_clean()

    def test_an_invented_category_is_treated_as_unknown_not_printed(self):
        """Choices only bind forms. An Excel import writes straight past them.

        The failure this guards is the quiet one: a typo reaching the schedule as though it
        were a box on the form. Unclassified is the truthful answer and makes the report
        flag the row instead.
        """
        self.instrument.cgt_asset_category_override = 'Shares in Aus listed companys'
        self.assertEqual(
            cgt.asset_category(self.instrument), CGTAssetCategory.UNCLASSIFIED)

    def test_an_invalid_override_does_not_silently_fall_back_to_the_derivation(self):
        """Ignoring it would discard what the user meant and look like it worked."""
        self.instrument.cgt_asset_category_override = 'nonsense'
        self.assertNotEqual(
            cgt.asset_category(self.instrument),
            CGTAssetCategory.AU_LISTED_SHARES)

    def test_an_empty_override_is_not_an_override(self):
        for empty in (None, ''):
            with self.subTest(empty=empty):
                self.instrument.cgt_asset_category_override = empty
                self.assertEqual(
                    cgt.asset_category(self.instrument),
                    CGTAssetCategory.AU_LISTED_SHARES)


class VocabularyTests(TransactionTestCase):
    """The point of moving every fixed vocabulary into one module.

    Ten choice fields were lists of tuples on model classes, and everything that reasoned
    about them -- the whole cgt package, the signals, the commands -- compared against bare
    string literals. Nothing checks a literal, so a rename or a missed member failed
    silently, and here that means a wrong number on a tax return rather than an exception.
    """

    def setUp(self):
        self.account = create_account()

    def test_an_unrecognised_taxpayer_type_is_treated_as_undeclared(self):
        """The bug this refactor existed to remove.

        `_RATE_BY_TAXPAYER_TYPE.get(taxpayer_type, FULL_DISCOUNT_RATE)` gave the full 50% to
        anything it did not recognise. Add a taxpayer type to the model and forget it in
        that dict and every gain that entity makes is quietly halved.
        """
        self.account.taxpayer_type = 'DECEASED_ESTATE'
        self.assertEqual(
            cgt.discount.taxpayer_type_of(self.account), choices.TaxpayerType.UNDECLARED)

    def test_a_missing_rate_raises_rather_than_defaulting(self):
        """The property that actually matters, stated directly.

        Testing that the lookup helper works does not test that `base_rate` uses it without
        a fallback -- a mutation restoring the old `.get(type, FULL_DISCOUNT_RATE)` passed
        every other test here. Removing an entry and requiring a KeyError is what pins it:
        a taxpayer type with no rate must fail loudly, not quietly become 50%.
        """
        from share_dinkum_app.cgt.discount import _RATE_BY_TAXPAYER_TYPE, base_rate
        self.account.taxpayer_type = choices.TaxpayerType.SMSF

        with patch.dict(_RATE_BY_TAXPAYER_TYPE, clear=False):
            del _RATE_BY_TAXPAYER_TYPE[choices.TaxpayerType.SMSF]
            with self.assertRaises(KeyError):
                base_rate(self.account)

        # And with the entry present it is the superannuation rate, not the full discount.
        self.assertEqual(base_rate(self.account), Decimal(1) / Decimal(3))

    def test_every_declared_taxpayer_type_has_a_rate(self):
        """An exhaustiveness check, which a dict with a default cannot give you."""
        from share_dinkum_app.cgt.discount import _RATE_BY_TAXPAYER_TYPE
        self.assertEqual(
            set(_RATE_BY_TAXPAYER_TYPE), set(choices.TaxpayerType))

    def test_the_asset_category_stores_a_code_and_reads_as_the_ato_wording(self):
        """Value and label are now separate, which is what makes rewording safe."""
        self.assertEqual(
            choices.CGTAssetCategory.AU_LISTED_SHARES.value, 'AU_LISTED_SHARES')
        self.assertEqual(
            choices.CGTAssetCategory.AU_LISTED_SHARES.label,
            'Shares in Australian listed companies')

    def test_choice_values_survived_the_refactor_unchanged(self):
        """Nothing stored in the database moved, which is why there is no data migration
        for any field but the asset category."""
        self.assertEqual(
            [c.value for c in choices.ResidencyStatus],
            ['RESIDENT', 'FOREIGN', 'TEMPORARY'])
        self.assertEqual(
            [c.value for c in choices.SellStrategy],
            ['FIFO', 'LIFO', 'MIN_CGT', 'MANUAL'])
        self.assertEqual(
            [c.value for c in choices.LegalFormSource],
            ['DEFAULT', 'SUGGESTED', 'USER'])
        self.assertEqual([c.value for c in choices.CGTBasis], ['LEGACY', 'DIVISION_115'])

    def test_the_models_expose_the_shared_vocabulary(self):
        """Both sides use one definition, so they cannot drift apart."""
        self.assertEqual(
            [v for v, _ in Account._meta.get_field('taxpayer_type').choices],
            [c.value for c in choices.TaxpayerType])
        self.assertEqual(
            [v for v, _ in ResidencyPeriod._meta.get_field('status').choices],
            [c.value for c in choices.ResidencyStatus])


class VocabularyPortfolioTests(TransactionTestCase):
    """The two vocabulary cases that need a portfolio of their own."""

    def test_an_unrecognised_type_is_flagged_rather_than_silently_discounted(self):
        """It still gets 50%, because that is what an undeclared account has always had.

        The difference is that the schedule now says so, instead of the figure resting on a
        lookup that missed.
        """
        data = create_golden_master_portfolio()
        account = data['account']
        Account.objects.filter(pk=account.pk).update(taxpayer_type='DECEASED_ESTATE')
        account.refresh_from_db()

        schedule = cgt.build_schedule(account, 'FY2023/24')
        self.assertIn(
            'does not say who owns this portfolio', ' '.join(schedule.warnings))

    def test_the_event_report_shows_the_wording_not_the_code(self):
        """A person filling in a schedule is looking for the box, not our identifier."""
        data = create_golden_master_portfolio()
        data['instrument'].legal_form = choices.LegalForm.COMPANY
        data['instrument'].save()
        data['instrument'].market.country = 'AU'
        data['instrument'].market.save()

        df = CGTEventReport(account=data['account']).generate()
        categories = set(str(v) for v in df['asset_category'])
        self.assertIn('Shares in Australian listed companies', categories)
        self.assertNotIn('AU_LISTED_SHARES', categories)


class PreDepartureDisposalTests(TransactionTestCase):
    """A sale made before leaving Australia was never inside the I1 deeming.

    s104-165(3) deems the assets you *held at the moment you ceased residency* to be taxable
    Australian property. Something you sold years earlier was not among them. The deeming
    was being applied on acquisition date alone, so a share bought in 2015 and sold in 2020
    -- while still resident, eleven years before any departure -- came back as TAP.

    No figure moved, because a gain is only ever disregarded when the holder was a foreign
    resident on the day of the event, and here they were not. But the status was reported on
    every such row, and it was wrong.
    """

    def setUp(self):
        self.account = create_account()
        self.instrument = create_instrument(account=self.account)
        declare(self.account, 'RESIDENT', date(2000, 1, 1), date(2021, 6, 30))
        declare(self.account, 'FOREIGN', date(2021, 7, 1), i1=True)
        self.declared = cgt.residency.periods(self.account)

    def _status(self, acquired, sold):
        return cgt.parcel_tap_status(
            self.account, self.instrument, acquired, sold, declared=self.declared)

    def test_a_share_sold_before_departure_is_not_deemed_taxable_australian_property(self):
        self.assertEqual(self._status(date(2015, 1, 1), date(2020, 1, 1)), cgt.tap.NTAP)

    def test_the_question_does_not_change_the_outcome_for_a_resident(self):
        """Whichever answer TAP takes, a resident is taxed on the gain.

        s855-10 only disregards a gain for a foreign or temporary resident, so for a
        disposal while resident the status is descriptive rather than operative -- which is
        why the bug moved no figure.
        """
        disregarded, reason = cgt.disregard(
            self.account, cgt.tap.NTAP, date(2020, 1, 1), declared=self.declared)
        self.assertFalse(disregarded)
        self.assertIsNone(reason)

    def test_a_share_still_held_at_departure_is_deemed_taxable_australian_property(self):
        self.assertEqual(self._status(date(2015, 1, 1), date(2026, 1, 1)), cgt.tap.TAP)

    def test_a_share_bought_after_departure_is_not(self):
        self.assertEqual(self._status(date(2022, 1, 1), date(2026, 1, 1)), cgt.tap.NTAP)

    def test_a_sale_on_the_day_of_departure_is_inside_the_deeming(self):
        """The boundary. Departure day is the first day of foreign residency, and an asset
        disposed of on it was still held when residency ceased."""
        self.assertEqual(self._status(date(2015, 1, 1), date(2021, 7, 1)), cgt.tap.TAP)

    def test_a_sale_the_day_before_departure_is_outside_it(self):
        self.assertEqual(self._status(date(2015, 1, 1), date(2021, 6, 30)), cgt.tap.NTAP)


class CarryForwardYearScopeTests(TransactionTestCase):
    """A capital loss is available against later years, and only later years.

    Prior losses were summed across every carry-forward row regardless of when the loss
    arose, so a loss made in 2026 would have been applied to a schedule for 2021 -- a
    deduction claimed years before it existed. It went unnoticed because the table was empty.
    """

    def setUp(self):
        self.data = create_golden_master_portfolio()
        self.account = self.data['account']
        self.earlier = FiscalYear.objects.get(name='FY2023/24')
        self.later = FiscalYear.objects.get(name='FY2024/25')

    def _record(self, fiscal_year, amount):
        return CapitalLossCarryForward.objects.create(
            account=self.account, fiscal_year=fiscal_year,
            amount=Money(Decimal(amount), 'AUD'), is_opening_balance=True)

    def test_a_loss_is_applied_to_a_later_year(self):
        self._record(self.earlier, '1000')
        schedule = cgt.build_schedule(self.account, self.later)
        # FY2024/25 is all losses in this fixture, so nothing absorbs it and it rolls on.
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('0'))
        self.assertEqual(
            schedule.losses_carried_forward.amount, Decimal('1568.6881') + Decimal('1000'))

    def test_a_loss_from_a_later_year_is_not_applied_to_an_earlier_one(self):
        """The bug. FY2023/24 must not benefit from a loss made in FY2024/25."""
        self._record(self.later, '1000')
        schedule = cgt.build_schedule(self.account, self.earlier)
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('0'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('2200.68085'))

    def test_a_loss_from_the_same_year_is_not_double_counted(self):
        """The year's own losses are already in the current-year pool."""
        self._record(self.earlier, '1000')
        schedule = cgt.build_schedule(self.account, self.earlier)
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('0'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('2200.68085'))

    def test_an_earlier_loss_reduces_a_later_gain(self):
        older = FiscalYear.objects.filter(start_year__lt=self.earlier.start_year).first()
        if older is None:
            self.skipTest('no earlier fiscal year in the fixture')
        self._record(older, '1000')
        schedule = cgt.build_schedule(self.account, self.earlier)
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('1000'))
        self.assertEqual(schedule.net_capital_gain.amount, Decimal('1700.68085'))

    def test_an_explicit_override_still_wins(self):
        """The what-if path, which does not consult the table at all."""
        self._record(self.later, '5000')
        schedule = cgt.build_schedule(
            self.account, self.earlier, prior_year_losses=Money(Decimal('1000'), 'AUD'))
        self.assertEqual(schedule.prior_year_losses_applied.amount, Decimal('1000'))
