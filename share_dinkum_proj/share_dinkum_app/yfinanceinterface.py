import yfinance as yf
from datetime import date, timedelta
from decimal import Decimal
import pandas as pd
import string


import logging
logger = logging.getLogger(__name__)



def to_snake_case(text):
    allowable_chars = string.ascii_letters + string.digits
    snake_case = ''.join([char if char in allowable_chars else '_' for char in text]).lower()
    return snake_case


def get_instrument_price_history(instrument, start_date):
    
    ticker_code = instrument.yfinance_ticker_code
    print(ticker_code)

    try:
        yfinance_obj = yf.Ticker(ticker_code)
        price_history = yfinance_obj.history(start=start_date)
        price_history = price_history.reset_index() # set the date as a column

        price_history.columns = [to_snake_case(col) for col in price_history.columns]

        price_history['instrument'] = instrument

        price_history['date'] = price_history['date'].apply(lambda x : x.date())

        price_history['open'] = pd.to_numeric(price_history['open'], errors='coerce')
        price_history['high'] = pd.to_numeric(price_history['high'], errors='coerce')
        price_history['low'] = pd.to_numeric(price_history['low'], errors='coerce')
        price_history['close'] = pd.to_numeric(price_history['close'], errors='coerce')
        price_history['volume'] = pd.to_numeric(price_history['volume'], errors='coerce')

        price_history['stock_splits'] = pd.to_numeric(price_history['stock_splits'], errors='coerce')

        price_history = price_history[['instrument', 'date', 'open', 'high', 'low', 'close', 'volume', 'stock_splits']]
        
        return price_history

    except Exception as e:
        print(f"Error fetching data for {ticker_code}: {e}")
        return pd.DataFrame([])


def get_exchange_rate_history(convert_from, convert_to, start_date):
    
    ticker_code = f'{convert_from}{convert_to}=X'
    yfinance_obj = yf.Ticker(ticker_code)
    print(ticker_code)

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
        
        return price_history


    except Exception as e:
        print(f"Error fetching data for {ticker_code}: {e}")
        return pd.DataFrame([])


def get_exchange_rate(convert_from, convert_to, exchange_date):
    ticker_code = f'{convert_from}{convert_to}=X'
    yfinance_obj = yf.Ticker(ticker_code)
    exchange_date = date.fromisoformat(str(exchange_date))
    # Get a few days in case of gaps, then take first record.
    start_date_str = exchange_date.isoformat()
    end_date_str = (exchange_date + timedelta(days=3)).isoformat()
    try:
        exchange_rate_history = yfinance_obj.history(start=start_date_str, end=end_date_str)
        return Decimal(exchange_rate_history['Close'].values[0])
    except Exception as e:
        print(e)
        return None
