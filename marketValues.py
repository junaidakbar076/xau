import pandas as pd
import ta  # Technical Analysis library
from datetime import datetime
import MetaTrader5 as mt5  # if you use MT5

# Example: fetch latest OHLC data from MT5

def get_ohlc(symbol="XAUUSD", timeframe=mt5.TIMEFRAME_M15, n=100):
    """
    Fetches OHLC data from MT5 and returns a DataFrame
    """
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    
    if rates is None or len(rates) == 0:
        raise ValueError("No data fetched from MT5")

    # Convert structured array to DataFrame
    df = pd.DataFrame(rates)

    # Ensure column names are correct
    df.rename(columns={
        'time': 'time',
        'open': 'open',
        'high': 'high',
        'low': 'low',
        'close': 'close',
        'tick_volume': 'tick_volume',
        'spread': 'spread',
        'real_volume': 'real_volume'
    }, inplace=True)

    # Convert timestamp to datetime
    df['time'] = pd.to_datetime(df['time'], unit='s')

    return df

# Calculate ATR and support/resistance
def calculate_dynamic_tp_sl(df, entry_price, risk_reward=2, lookback=20):
    """
    df: OHLC dataframe
    entry_price: float, price of entry
    risk_reward: ratio for TP vs SL
    lookback: number of candles for support/resistance
    """
    # ATR calculation (14-period by default)
    df['ATR'] = ta.volatility.AverageTrueRange(
        high=df['high'], low=df['low'], close=df['close'], window=14
    ).average_true_range()

    atr = df['ATR'].iloc[-1]

    # Recent support and resistance
    recent_high = df['high'][-lookback:].max()
    recent_low = df['low'][-lookback:].min()

    # Stop Loss (for long position)
    sl = max(entry_price - atr, recent_low)  # never below recent support
    # Take Profit
    tp = max(entry_price + atr * risk_reward, recent_high + atr * 0.5)  # buffer above resistance

    return tp, sl

# Example usage
def main():
    if not mt5.initialize():
        print("MT5 init failed")
    else:
        df = get_ohlc("XAUUSD", mt5.TIMEFRAME_M15, 100)
        print(df.head())
        entry_price = df['close'].iloc[-1]  # current market price
        tp, sl = calculate_dynamic_tp_sl(df, entry_price)
        print(f"Take Profit: {tp}, Stop Loss: {sl}")
        return tp, sl
   

if __name__ == "__main__":
    main()