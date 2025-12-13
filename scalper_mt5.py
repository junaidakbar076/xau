
"""
scalper_mt5.py
Merged XAUUSD scalping bot that connects to a local MetaTrader 5 terminal and blocks trading around high-impact news.
- Uses EMA crossover + RSI filter
- Integrates live news check via MyFxBook API (user provides credentials in .env)
- Falls back to MT5 economic calendar if MyFxBook is unavailable
- Logs trades to CSV

USAGE:
1) Fill .env with your MyFxBook credentials and desired settings (see .env.example)
2) Install dependencies: pip install -r requirements.txt
3) Start MetaTrader 5 desktop and log into your demo account
4) Run: python scalper_mt5.py
"""
import os
from pathlib import Path
import time
import math
from datetime import datetime, timezone, timedelta
import csv
from marketValues import main as marketValues
import traceback
from bot import update_median_spread, is_spread_ok
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from dotenv import load_dotenv
load_dotenv()
# Local import for news integration
from news_integration import is_gold_news_now, init_news_session

# ---------- CONFIG (can be overridden via environment variables) ----------
SYMBOL = os.getenv("SYMBOL", "XAUUSD")
TIMEFRAME = mt5.TIMEFRAME_M1  # 1-minute timeframe
LOT_MIN = float(os.getenv("LOT_MIN", "0.01"))
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "3.0"))
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.25"))  # percent of equity per trade
SL_PIPS = float(os.getenv("SL_PIPS", "30"))   # gold uses larger SL (pips here = USD cents for XAUUSD)
TP_PIPS = float(os.getenv("TP_PIPS", "45"))
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_SLOW = int(os.getenv("EMA_SLOW", "21"))
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "5"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "10"))
MAGIC = int(os.getenv("MAGIC", "987654"))
LOG_CSV = os.getenv("LOG_CSV", "trades_log.csv")
NEWS_BLOCK_WINDOW_MIN = int(os.getenv("NEWS_BLOCK_WINDOW_MIN", "45"))  # typically 45 minutes
# ------------------------------------------------------------------------

def pips_to_price(symbol, pips):
    """Convert pips to price difference depending on symbol digits.
       For XAUUSD brokers often quote with 2 or 3 decimals; treat 'pip' here as 0.01 USD."""
    info = mt5.symbol_info(symbol)
    if info is None:
        raise RuntimeError("Symbol info not found.")
    # Treat 1 pip as 0.01 for XAUUSD by default
    return pips * 0.01

def get_spread_pips(symbol):
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)
    if not tick or not info:
        return float("inf")
    # For XAUUSD, use 0.01 as pip unit
    spread = (tick.ask - tick.bid) / 0.01
    return spread

def calculate_lots(symbol, sl_pips, risk_percent):
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("No account info.")
    equity = account.equity
    risk_amount = equity * (risk_percent / 500.0)
    # pip value approx: for XAUUSD 1 lot (100 oz) pip (0.01 USD) value ~1 USD
    # This is broker-dependent. The user should verify and adjust pip_value_per_lot.
    pip_value_per_lot = float(os.getenv("PIP_VALUE_PER_LOT", "0.1"))
    lot = 0.01#(risk_amount / (sl_pips * pip_value_per_lot))
    info = mt5.symbol_info(symbol)
    if info is None:
        return LOT_MIN
    step = info.volume_step or 0.01
    lot = max(LOT_MIN, lot)
    # round down to nearest step
    lot = math.floor(lot / step) * step
    # round to 2 decimal places
    return round(lot, 2)

def fetch_ohlcv(symbol, timeframe, n=500):
    utc_to = datetime.now(timezone.utc)
    utc_from = utc_to - timedelta(days=3)
    rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

def signal_from_df(df):
    close = df['close']
    if len(close) < EMA_SLOW + 3:
        return None
    ema_fast = EMAIndicator(close=close, window=EMA_FAST).ema_indicator()
    ema_slow = EMAIndicator(close=close, window=EMA_SLOW).ema_indicator()
    rsi = RSIIndicator(close=close, window=RSI_PERIOD).rsi()
    # detect cross using last two bars
    if (ema_fast.iloc[-2] <= ema_slow.iloc[-2]) and (ema_fast.iloc[-1] > ema_slow.iloc[-1]) and (40 < rsi.iloc[-1] < 70):
        return "BUY"
    if (ema_fast.iloc[-2] >= ema_slow.iloc[-2]) and (ema_fast.iloc[-1] < ema_slow.iloc[-1]) and (30 < rsi.iloc[-1] < 60):
        return "SELL"
    return None

def send_order(symbol, action, lot, sl_pips, tp_pips):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print("tick data not available.")
        return None
    price = tick.ask if action == "BUY" else tick.bid
    deviation = 50  # allowed slippage in points
    info = mt5.symbol_info(symbol)
    point = info.point if info else 0.01
    if action == "BUY":
        sl = price - pips_to_price(symbol, sl_pips)
        tp = price + pips_to_price(symbol, tp_pips)
        order_type = mt5.ORDER_TYPE_BUY
    else:
        sl = price + pips_to_price(symbol, sl_pips)
        tp = price - pips_to_price(symbol, tp_pips)
        order_type = mt5.ORDER_TYPE_SELL

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": deviation,
        "magic": MAGIC,
        "comment": "xau_scalper_py",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    return result

def log_trade(row):
    header = ["timestamp","symbol","action","volume","price","sl","tp","result_code","comment"]
    write_header = not Path(LOG_CSV).exists()
    with open(LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerow(row)

def main_loop():
    print("Initializing news session (if configured)...")
    init_news_session()  # prepare MyFxBook session if possible

    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        raise SystemExit
    print("MT5 initialized.")

    if not mt5.symbol_select(SYMBOL, True):
        print(f"Failed to select {SYMBOL}")
        mt5.shutdown()
        raise SystemExit
    # Get current positions for this symbol
    positions = mt5.positions_get(symbol=SYMBOL)
    if positions is None:
        positions = []

    try:
        while True:
            try:
                time.sleep(SLEEP_SECONDS)
                update_median_spread(SYMBOL)

                if not is_spread_ok(SYMBOL):
                    print("Spread too high — delaying.")
                    time.sleep(5)
                    continue
                # Check news first (blocks trading if high-impact around now)
                try:
                    if is_gold_news_now(window_minutes=NEWS_BLOCK_WINDOW_MIN):
                        print(f"{datetime.now(timezone.utc)} - Trading blocked due to gold-related news.")
                        continue
                except Exception as e:
                    print("News check failed:", e)
                    # proceed but be cautious; you may choose to continue or skip when news check fails

                # Check spread
                spread = get_spread_pips(SYMBOL)
                if spread > MAX_SPREAD_PIPS:
                    print(f"{datetime.now(timezone.utc)} - Spread too high: {spread:.2f} pips/{MAX_SPREAD_PIPS}. Skipping.")
                    continue

                # Fetch candles
                df = fetch_ohlcv(SYMBOL, TIMEFRAME)
                if df is None or df.empty:
                    print("No price data returned.")
                    continue
                signal = signal_from_df(df)
                # Close opposing positions if signal reversed
                if positions:
                    for pos in positions:
                        if (signal == "BUY" and pos.type == mt5.ORDER_TYPE_SELL) or (signal == "SELL" and pos.type == mt5.ORDER_TYPE_BUY):
                            close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
                            close_request = {
                                "action": mt5.TRADE_ACTION_DEAL,
                                "symbol": pos.symbol,
                                "volume": pos.volume,
                                "type": close_type,
                                "position": pos.ticket,
                                "price": mt5.symbol_info_tick(pos.symbol).bid if close_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(pos.symbol).ask,
                                "deviation": 50,
                                "magic": MAGIC,
                                "comment": "Auto-close due to opposite signal",
                                "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": mt5.ORDER_FILLING_IOC
                            }
                            close_res = mt5.order_send(close_request)
                            print(f"Closed opposing trade ticket {pos.ticket}, result: {close_res}")

                print(f"{datetime.now(timezone.utc)} - Signal: {signal} - Spread: {spread:.2f}")

                if signal is None:
                    continue
                # tp,sl= marketValues()
                sl=SL_PIPS
                tp=TP_PIPS
                lot = calculate_lots(SYMBOL, sl, RISK_PERCENT)
                if lot < LOT_MIN or sl < SL_PIPS or tp < TP_PIPS:
                    print("Computed lot too small. Skipping.")
                    continue
                positions = mt5.positions_get(symbol=SYMBOL)
                if positions is None:
                    positions = []

                # If no signal, skip
                if signal is None:
                    continue

                # If max trades reached, don't open more
                if len(positions) >= MAX_CONCURRENT_TRADES:
                    print("Max 10 trades running — waiting.")
                    continue

                # Open trade
                res = send_order(SYMBOL, signal, lot, sl, tp)
                print("Order send result:", res)

                if res is None:
                    print("Order failed.")
                else:
                    code = getattr(res, "retcode", None)
                    comment = getattr(res, "comment", "")
                    log_trade([datetime.now(timezone.utc).isoformat(), SYMBOL, signal, lot,
                            getattr(res, "price", None),
                            getattr(res, "sl", None),
                            getattr(res, "tp", None),
                            code, comment])

                    if code in (mt5.TRADE_RETCODE_DONE, 10009):
                        print("Trade executed successfully.")
                    else:
                        print("Trade failed:", code, comment)

                # Wait 1s before checking next signal and opening another trade
                time.sleep(1)

            except KeyboardInterrupt:
                print("Stopping by user.")
                break
            except Exception:
                print("Error in main loop:", traceback.format_exc())
                # Wait briefly before next iteration on error
                time.sleep(5)
    finally:
        mt5.shutdown()
        print("MT5 shutdown.")

if __name__ == "__main__":
    main_loop()
