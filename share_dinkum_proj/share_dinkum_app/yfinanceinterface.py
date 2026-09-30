import yfinance as yf
from datetime import date, timedelta, datetime, UTC
import pandas as pd
import string
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from share_dinkum_app.utils import convert_to_decimal


if TYPE_CHECKING:
    from share_dinkum_app.models import Instrument

import logging
logger = logging.getLogger(__name__)



def to_snake_case(text: str) -> str:
    allowable_chars = string.ascii_letters + string.digits
    snake_case = ''.join([char if char in allowable_chars else '_' for char in text]).lower()
    return snake_case


PRICE_COLUMNS = ['open', 'high', 'low', 'close']


def as_traded(price_history: pd.DataFrame) -> pd.DataFrame:
    """Undo the split adjustment Yahoo applies to its unadjusted prices.

    Even with `auto_adjust=False`, Yahoo's prices are divided by every split since (and its
    volumes multiplied), as of the day they are fetched: NVDA's close on 7 June 2024, before
    its 10-for-1 split, comes back as 120.89 rather than 1,208.88. Each row is multiplied back
    by the splits strictly after it, since a split's own row is already post-split. That is
    right only if the rows run to the day of the fetch, so every such split is among them.
    """
    price_history = price_history.sort_values('date').reset_index(drop=True)
    ratios = pd.to_numeric(price_history['stock_splits'], errors='coerce').fillna(0)
    ratios = ratios.where(ratios > 0, 1.0)
    later = ratios.iloc[::-1].cumprod().iloc[::-1].shift(-1, fill_value=1.0)
    for column in PRICE_COLUMNS:
        price_history[column] = price_history[column] * later
    price_history['volume'] = price_history['volume'] / later
    return price_history


def get_instrument_price_history(instrument: 'Instrument', start_date: date | datetime | str | None,
                                 end_date: date | datetime | str | None = None) -> pd.DataFrame:
    """Daily prices as traded that day, from `start_date` to `end_date` (default today).

    Not adjusted for dividends or splits: every use of a stored price asks what a unit was
    worth on the day, whether for a deemed disposal or for the value of a holding counted in
    that day's units. An adjusted close answers a different question (total return), and
    its dividend adjustment understated a 30 June value by any distribution going ex on
    1 July, as most ASX funds' June distributions do.
    """
    ticker_code = instrument.yfinance_ticker_code
    logger.info('Fetching price history for %s', ticker_code)

    try:
        if start_date is None:
            logger.warning('Skipping price history for %s; start_date was not provided.', ticker_code)
            return pd.DataFrame([])

        if isinstance(start_date, datetime):
            start_date = start_date.date()
        elif isinstance(start_date, str):
            start_date = date.fromisoformat(start_date)
        elif not isinstance(start_date, date):
            raise TypeError('start_date must be a date, datetime, or ISO formatted string.')

        if end_date is not None:
            if isinstance(end_date, datetime):
                end_date = end_date.date()
            elif isinstance(end_date, str):
                end_date = date.fromisoformat(end_date)
            elif not isinstance(end_date, date):
                raise TypeError('end_date must be a date, datetime, or ISO formatted string.')

            if end_date < start_date:
                logger.warning(
                    'Skipping price history for %s; end_date %s is before start_date %s',
                    ticker_code,
                    end_date,
                    start_date,
                )
                return pd.DataFrame([])

        # Fetched to today whatever the end date, so every split that Yahoo has already
        # divided these prices by is in the rows and can be multiplied back out. Rows after
        # the end date are dropped once that is done.
        history_kwargs: dict[str, Any] = {'start': start_date.isoformat(), 'auto_adjust': False}
        today = date.today()
        if end_date and end_date < today:
            history_kwargs['end'] = (today + timedelta(days=1)).isoformat()
        elif end_date:
            history_kwargs['end'] = (end_date + timedelta(days=1)).isoformat()

        yfinance_obj = yf.Ticker(ticker_code)
        price_history = yfinance_obj.history(**history_kwargs)
        price_history = price_history.reset_index() # set the date as a column

        price_history.columns = [to_snake_case(col) for col in price_history.columns]

        price_history['instrument'] = instrument

        price_history['date'] = price_history['date'].apply(lambda x : x.date())

        # Handle the case of these cols not being returned
        if 'volume' not in price_history.columns:
            price_history['volume'] = 0

        if 'stock_splits' not in price_history.columns:
            price_history['stock_splits'] = 0

        for column in [*PRICE_COLUMNS, 'volume', 'stock_splits']:
            price_history[column] = pd.to_numeric(price_history[column], errors='coerce')

        price_history = as_traded(price_history)
        if end_date:
            price_history = price_history[price_history['date'] <= end_date].copy()
        price_history['volume'] = price_history['volume'].fillna(0).round()

        price_history = price_history[['instrument', 'date', 'open', 'high', 'low', 'close', 'volume', 'stock_splits']]

        for col in ['open', 'high', 'low', 'close', 'stock_splits']:
            price_history[col] = price_history[col].apply(lambda val: convert_to_decimal(val, 16, 6))  # type: ignore[arg-type, return-value]
            
        return price_history

    except Exception as e:
        logger.error(f"Error fetching data for {ticker_code}: {e}", exc_info=True)
        return pd.DataFrame([])



def get_current_price(instrument: 'Instrument') -> Decimal | None:
    """Fetch live/current price from yfinance ticker info."""
    ticker_code = instrument.yfinance_ticker_code
    try:
        ticker = yf.Ticker(ticker_code)
        info = ticker.info
        price = info.get('currentPrice') or info.get('regularMarketPrice')
        if price is not None:
            return convert_to_decimal(price, 16, 6)
        return None
    except Exception as e:
        logger.warning('Could not fetch current price for %s: %s', ticker_code, e)
        return None


def get_exchange_rate_history(convert_from: str, convert_to: str, start_date: date) -> pd.DataFrame:
    
    ticker_code = f'{convert_from}{convert_to}=X'
    yfinance_obj = yf.Ticker(ticker_code)
    logger.info('Fetching exchange rate history for %s', ticker_code)

    try:
        yfinance_obj = yf.Ticker(ticker_code)
        price_history = yfinance_obj.history(start=start_date)
        price_history = price_history.reset_index() # set the date as a column

        price_history.columns = [to_snake_case(col) for col in price_history.columns]

        price_history['convert_from'] = convert_from
        price_history['convert_to'] = convert_to
        price_history['date'] = price_history['date'].apply(lambda x : x.date())
        price_history['exchange_rate_multiplier'] = pd.to_numeric(price_history['close'], errors='coerce')
        price_history = price_history[['convert_from', 'convert_to', 'date', 'exchange_rate_multiplier']]

        price_history['is_continuous_history'] = True
        price_history['exchange_rate_multiplier'] = price_history['exchange_rate_multiplier'].apply(
            lambda val: convert_to_decimal(val, 16, 6)
        )
        
        return price_history


    except Exception as e:
        logger.error(f"Error fetching exchange rate for {ticker_code}: {e}", exc_info=True)
        logger.error('Try to update yfinance package to latest version if the issue persists.')
        return pd.DataFrame([])


def get_exchange_rate(convert_from: str, convert_to: str, exchange_date: date | str | None = None) -> Decimal | None:
    ticker_code = f'{convert_from}{convert_to}=X'
    yfinance_obj = yf.Ticker(ticker_code)

    today = datetime.now(UTC).date()
    if not exchange_date:
        exchange_date = today

    exchange_date = date.fromisoformat(str(exchange_date))

    # No market data can exist for a future date (usually a data-entry error).
    # Fall back to today's rate rather than silently failing, and flag it.
    # Allow a one-day grace: markets ahead of UTC (e.g. ASX in Sydney UTC+10/+11,
    # users entering in AWST UTC+8) can legitimately be on "tomorrow" relative to
    # UTC near the date boundary, so only dates clearly in the future are clamped.
    if exchange_date > today + timedelta(days=1):
        logger.warning(
            'Requested exchange rate date %s for %s is in the future; using current date %s instead.',
            exchange_date, ticker_code, today,
        )
        exchange_date = today

    # The day's rate, or the last one before it where there was none (a weekend, a holiday):
    # a rate that was known on the day, as the history refresh also uses for a stand-in. This
    # used to take the next trading day's, so 30 June on a Sunday got Monday's rate.
    start_date_str = (exchange_date - timedelta(days=7)).isoformat()
    end_date_str = (exchange_date + timedelta(days=1)).isoformat()
    try:
        exchange_rate_history = yfinance_obj.history(start=start_date_str, end=end_date_str)
        close_values = exchange_rate_history['Close'].dropna().values
        if len(close_values) == 0:
            logger.warning('No exchange rate data returned for %s between %s and %s', ticker_code, start_date_str, end_date_str)
            return None
        return convert_to_decimal(close_values[-1], 16, 6)
    except Exception as e:
        logger.error(f"Error fetching exchange rate for {ticker_code}: {e}", exc_info=True)
        logger.error('Try to update yfinance package to latest version if the issue persists.')
        return None
