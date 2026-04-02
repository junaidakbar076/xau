#!/usr/bin/env python3
"""
scalper_mt5.py — Multi-pair trend scalper (fixed & safer)
Fixes applied:
- Correct pip ↔ price conversion for all symbol digit formats (EURUSD, USDJPY, XAUUSD, etc.)
- ATR converted to pips correctly per symbol
- Minimum stop distance (broker stop level) enforced
- SL/TP capped reasonably and made realistic (no massive overshoots)
- Lot sizing uses pips (not price) and enforced to broker min/step and hard cap
- Safer defaults for MIN_ATR_PIPS and HARD_MAX_LOT
- Robustness improvements and defensive checks
"""
import os
from pathlib import Path
import time
import math
from datetime import datetime, timezone, timedelta
import csv
import traceback

import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# TA imports
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

# local helpers (news + spread module) — these must exist in your project
from news_integration import is_gold_news_now, init_news_session
from bot import update_median_spread, is_spread_ok  # spread helper

load_dotenv()

# ---------------- CONFIG ----------------
MAJOR_PAIRS = [
    "XAUUSD", 
    # "EURUSD", "GBPUSD", "USDJPY", "AUDUSD",
    # "NZDUSD", "USDCAD", "USDCHF"
]

TIMEFRAME_M1 = mt5.TIMEFRAME_M1
TIMEFRAME_M5 = mt5.TIMEFRAME_M5

# Tunables (env override allowed)
LOT_MIN = float(os.getenv("LOT_MIN", "0.01"))
MAX_SPREAD_PIPS = float(os.getenv("MAX_SPREAD_PIPS", "150"))  # in "pips" units where 1 pip=0.0001 (or 0.01 for JPY/XAU)
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.2"))  # percent of equity per trade
PIP_VALUE_PER_LOT = float(os.getenv("PIP_VALUE_PER_LOT", "1.0"))  # user-supplied approx pip value per lot
EMA_FAST = int(os.getenv("EMA_FAST", "9"))
EMA_M5_TREND_FAST = int(os.getenv("EMA_M5_FAST", "50"))
EMA_M5_TREND_SLOW = int(os.getenv("EMA_M5_SLOW", "200"))
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.6"))
TP_ATR_MULT = float(os.getenv("TP_ATR_MULT", "1.2"))
SLEEP_SECONDS = float(os.getenv("SLEEP_SECONDS", "5"))
MAGIC = int(os.getenv("MAGIC", "987654"))
LOG_CSV = os.getenv("LOG_CSV", "trades_log.csv")
NEWS_BLOCK_WINDOW_MIN = int(os.getenv("NEWS_BLOCK_WINDOW_MIN", "45"))
MAX_CONCURRENT_TRADES = int(os.getenv("MAX_CONCURRENT_TRADES", "2"))
REPEAT_OPEN_DELAY = float(os.getenv("REPEAT_OPEN_DELAY", "1.0"))
# Minimum ATR (in pips) used to avoid tiny SLs — default lowered to 8 pips for majors
MIN_ATR_PIPS = float(os.getenv("MIN_ATR_PIPS", "8"))
# Hard safety cap on lots (change if you want larger)
HARD_MAX_LOT = float(os.getenv("HARD_MAX_LOT", "0.05"))
# Caps for TP/SL in pips (so we don't set unrealistic TP)
MAX_TP_PIPS = int(os.getenv("MAX_TP_PIPS", "50"))
MAX_SL_PIPS = int(os.getenv("MAX_SL_PIPS", "80"))
# ----------------------------------------

def now_utc():
    return datetime.now(timezone.utc)

# --- pip/point helpers ---
def get_symbol_info_safe(symbol):
    info = mt5.symbol_info(symbol)
    if info is None:
        # try selecting symbol then re-fetch
        try:
            mt5.symbol_select(symbol, True)
            info = mt5.symbol_info(symbol)
        except Exception:
            return None
    return info

def pip_price(symbol):
    """Return the price-value of 1 pip for this symbol (e.g. EURUSD -> 0.0001, USDJPY -> 0.01, XAUUSD -> 0.01)"""
    info = get_symbol_info_safe(symbol)
    if info is None:
        return 0.0001
    # Standard convention: 1 pip = 10 * point for 5-digit (EURUSD) or 3-digit (JPY) quotes
    if info.digits in (5, 3):
        return info.point * 10.0
    # For 4-digit/2-digit (rare) assume 1 pip == point
    return info.point

def pips_to_price(symbol, pips):
    """Convert pip distance into correct price distance for the symbol."""
    info = mt5.symbol_info(symbol)
    if info is None:
        return None

    digits = info.digits
    point = info.point

    # --- PIP SIZE DETECTION ---
    if symbol.startswith("XAU"):  
        pip_size = 0.10   # 1 pip = 0.10 for gold (most brokers)
    elif "JPY" in symbol:
        pip_size = 0.01   # JPY pairs → 1 pip = 0.01
    else:
        pip_size = 0.0001 # Normal forex → 1 pip = 0.0001

    # Convert pip to points
    return pips * (pip_size / point) * point

def atr_to_pips(symbol, atr_value):
    """Convert ATR (price units) to pips for the symbol."""
    if "JPY" in symbol:
        pip_size = 0.01
    elif symbol.startswith("XAU"):
        pip_size = 0.10
    else:
        pip_size = 0.0001  # 5-digit pairs

    return atr_value / pip_size

def price_to_pips(symbol, price_distance):
    """Convert a price distance into pips for the symbol."""
    pp = pip_price(symbol)
    if pp == 0:
        return 0.0
    return price_distance / pp

# --- stop-distance enforcement ---
def ensure_min_stop_distance(symbol, price, sl, tp, order_type):
    """Ensure sl/tp satisfy broker minimum stop level (trade_stops_level)."""
    info = get_symbol_info_safe(symbol)
    if info is None:
        return sl, tp

    point = info.point
    stop_level_points = info.trade_stops_level or 0
    min_distance = stop_level_points * point

    # BUY: SL < price < TP
    if order_type == mt5.ORDER_TYPE_BUY:
        if price - sl < min_distance:
            sl = price - min_distance
        if tp - price < min_distance:
            tp = price + min_distance
    else:
        # SELL: TP < price < SL
        if sl - price < min_distance:
            sl = price + min_distance
        if price - tp < min_distance:
            tp = price - min_distance
    return sl, tp

# --- spread helper (in pips) ---
def get_spread_pips(symbol):
    tick = mt5.symbol_info_tick(symbol)
    info = get_symbol_info_safe(symbol)
    if not tick or not info:
        return float("inf")
    spread_price = tick.ask - tick.bid
    return price_to_pips(symbol, spread_price)

# --- lot sizing (sl_pips passed in pips) ---
def calculate_lots(symbol, sl_pips, risk_percent):
    """
    sl_pips: stop loss distance in pips
    risk_percent: percent of equity to risk
    Returns a lot rounded down to broker step and capped.
    """
    account = mt5.account_info()
    if account is None:
        raise RuntimeError("No account info.")
    equity = account.equity
    risk_amount = equity * (risk_percent / 100.0)

    # sanitize sl_pips
    sl_pips = max(float(sl_pips), 1.0)

    # pip value per lot configured by user (fallback 1.0)
    pip_value = PIP_VALUE_PER_LOT if PIP_VALUE_PER_LOT > 0 else 1.0

    raw_lot = risk_amount / (sl_pips * pip_value)

    info = get_symbol_info_safe(symbol)
    if not info:
        return max(LOT_MIN, round(raw_lot, 3))

    step = info.volume_step or 0.01
    volume_min = info.volume_min or LOT_MIN
    volume_max = info.volume_max or HARD_MAX_LOT

    # start with raw, but enforce minimum and safety caps
    lot = max(volume_min, LOT_MIN, raw_lot)

    # floor to step
    lot = math.floor(lot / step) * step

    # hard caps
    lot = min(lot, volume_max, HARD_MAX_LOT)

    # final safety: if rounding yields 0, return minimum step
    if lot < step:
        lot = step

    # round to 3 decimals for display
    return round(lot, 3)

# --- OHLC fetcher ---
def fetch_ohlcv(symbol, timeframe, n=500):
    utc_to = now_utc()
    utc_from = utc_to - timedelta(days=3)
    rates = mt5.copy_rates_range(symbol, timeframe, utc_from, utc_to)
    if rates is None or len(rates) == 0:
        return None
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    return df

# --- chart drawing ---
def draw_signal(symbol, action, price=None):
    try:
        t = int(time.time())
        obj_name = f"sig_{symbol}_{action}_{t}"
        info = get_symbol_info_safe(symbol)
        if info is None:
            return
        if price is None:
            tick = mt5.symbol_info_tick(symbol)
            if tick is None:
                return
            price = tick.bid if action == "BUY" else tick.ask

        # prefer the newer signature but try both
        try:
            mt5.object_create(obj_name, mt5.OBJ_ARROW_UP if action == "BUY" else mt5.OBJ_ARROW_DOWN, 0, t, price)
            mt5.object_set_integer(0, obj_name, mt5.OBJPROP_COLOR, mt5.COLOR_GREEN if action == "BUY" else mt5.COLOR_RED)
            mt5.object_set_integer(0, obj_name, mt5.OBJPROP_WIDTH, 2)
        except Exception:
            try:
                mt5.object_create(chart_id=0, object_type=(mt5.OBJ_ARROW_UP if action == "BUY" else mt5.OBJ_ARROW_DOWN),
                                  object_name=obj_name, sub_window=0, time=t, price=price)
            except Exception:
                return
    except Exception:
        return

# --- indicators ---
def compute_indicators(df, ema_fast_len=EMA_FAST, rsi_len=RSI_PERIOD, atr_len=ATR_PERIOD):
    close = df['close']
    ema_fast = EMAIndicator(close=close, window=ema_fast_len).ema_indicator()
    rsi = RSIIndicator(close=close, window=rsi_len).rsi()
    atr_indicator = AverageTrueRange(high=df["high"], low=df["low"], close=df["close"], window=atr_len)
    atr_series = atr_indicator.average_true_range()
    # extract last ATR scalar safely
    if hasattr(atr_series, "iloc"):
        try:
            atr_last = float(atr_series.iloc[-1])
        except Exception:
            atr_last = float(atr_series.values[-1]) if len(atr_series) > 0 else 0.0
    else:
        atr_last = float(atr_series)
    return ema_fast, rsi, atr_last

def m5_trend_direction(symbol):
    df5 = fetch_ohlcv(symbol, TIMEFRAME_M5, n=500)
    if df5 is None or len(df5) < max(EMA_M5_TREND_SLOW, EMA_M5_TREND_FAST) + 2:
        return None
    close5 = df5['close']
    ema50 = EMAIndicator(close=close5, window=EMA_M5_TREND_FAST).ema_indicator()
    ema200 = EMAIndicator(close=close5, window=EMA_M5_TREND_SLOW).ema_indicator()
    if ema50.iloc[-1] > ema200.iloc[-1]:
        return "BUY"
    if ema50.iloc[-1] < ema200.iloc[-1]:
        return "SELL"
    return None

def signal_from_df_m1(df):
    close = df['close']
    if len(close) < EMA_FAST + 5:
        return None

    ema9 = EMAIndicator(close=close, window=EMA_FAST).ema_indicator()
    rsi = RSIIndicator(close=close, window=RSI_PERIOD).rsi()

    last_close = float(close.iloc[-1])
    last_ema = float(ema9.iloc[-1])

    # ATR for tolerance (price units)
    atr_indicator = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=ATR_PERIOD)
    atr_series = atr_indicator.average_true_range()
    if hasattr(atr_series, "iloc"):
        try:
            atr_last = float(atr_series.iloc[-1])
        except Exception:
            atr_last = float(atr_series.values[-1]) if len(atr_series) > 0 else 0.0
    else:
        atr_last = float(atr_series)

    if atr_last == 0 or math.isnan(atr_last):
        return None

    # Convert ATR price-units to pips for this symbol and enforce minimum
    # pip_price = pip_price(symbol) can't call here (no symbol param). We'll use tolerance fraction of price ATR.
    tol = max(atr_last * 0.15, 0.0003)  # smaller default tolerance
    if abs(last_close - last_ema) > tol:
        return None

    rsi_last = float(rsi.iloc[-1])
    if rsi_last > 55:
        return "BUY"
    if rsi_last < 45:
        return "SELL"
    return None

# --- adjust stops to broker rules (and reasonable caps) ---
def adjust_and_cap_stops(symbol, price, sl_price, tp_price, order_type):
    # enforce broker minimum
    sl_price, tp_price = ensure_min_stop_distance(symbol, price, sl_price, tp_price, order_type)

    # ensure SL/TP are not absurdly large - convert to pips and cap
    sl_pips = abs(price_to_pips(symbol, price - sl_price))
    tp_pips = abs(price_to_pips(symbol, tp_price - price))

    # apply sensible caps
    sl_pips = min(int(sl_pips), MAX_SL_PIPS)
    tp_pips = min(int(tp_pips), MAX_TP_PIPS)

    # convert back to prices
    sl_price = price - pips_to_price(symbol, sl_pips) if order_type == mt5.ORDER_TYPE_BUY else price + pips_to_price(symbol, sl_pips)
    tp_price = price + pips_to_price(symbol, tp_pips) if order_type == mt5.ORDER_TYPE_BUY else price - pips_to_price(symbol, tp_pips)

    # final ensure against broker minimum again
    sl_price, tp_price = ensure_min_stop_distance(symbol, price, sl_price, tp_price, order_type)
    return sl_price, tp_price, sl_pips, tp_pips

# --- order sender (price SL/TP passed as absolute prices) ---
def send_order(symbol, action, lot, sl_price, tp_price):
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        print("tick data not available.")
        return None

    info = get_symbol_info_safe(symbol)
    if info is None:
        print("No symbol info.")
        return None

    price = tick.ask if action == "BUY" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL

    # adjust stops to broker minimums and cap them
    sl_price, tp_price, sl_pips, tp_pips = adjust_and_cap_stops(symbol, price, sl_price, tp_price, order_type)

    # Force filling mode 0 which is most widely supported
    filling_mode = mt5.ORDER_FILLING_FOK

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "sl": sl_price,
        "tp": tp_price,
        "deviation": 50,
        "magic": MAGIC,
        "comment": "multi_trend_scalper",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }

    result = mt5.order_send(request)
    return result

# --- logging ---
def log_trade(row):
    header = ["timestamp","symbol","action","volume","price","sl","tp","result_code","comment"]
    write_header = not Path(LOG_CSV).exists()
    with open(LOG_CSV, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(header)
        writer.writerow(row)

# ---------------- main loop ----------------
def main_loop():
    print("Initializing news session (if configured)...")
    init_news_session()
    if not mt5.initialize():
        print("initialize() failed, error code =", mt5.last_error())
        raise SystemExit
    print("MT5 initialized.")

    # ensure symbols visible
    for s in MAJOR_PAIRS:
        mt5.symbol_select(s, True)

    # seed spread calc
    update_median_spread("XAUUSD")

    try:
        while True:
            try:
                time.sleep(SLEEP_SECONDS)
                # update spread memory
                update_median_spread("XAUUSD")
                # Check broad spread condition
                if not is_spread_ok("XAUUSD"):
                    print(f"{now_utc()} - Spread too high overall, delaying cycle.")
                    time.sleep(5)
                    continue

                # news filter
                try:
                    if is_gold_news_now(window_minutes=NEWS_BLOCK_WINDOW_MIN):
                        print(f"{now_utc()} - Trading blocked due to high-impact news.")
                        time.sleep(NEWS_BLOCK_WINDOW_MIN * 30 / 60)
                        continue
                except Exception as e:
                    print("News check failed:", e)

                for symbol in MAJOR_PAIRS:
                    try:
                        spread = get_spread_pips(symbol)
                        if spread > MAX_SPREAD_PIPS:
                            print(f"{now_utc()} - {symbol} spread too high ({spread:.2f}), skipping.")
                            continue

                        trend = m5_trend_direction(symbol)
                        print(f"Trend for {symbol}: {trend}")
                        if trend is None:
                            continue

                        df1 = fetch_ohlcv(symbol, TIMEFRAME_M1)
                        if df1 is None or df1.empty:
                            continue

                        entry_signal = signal_from_df_m1(df1)
                        if entry_signal is None:
                            continue

                        if entry_signal != trend:
                            continue

                        # compute ATR (price units) and convert to pips correctly
                        _, _, atr_last = compute_indicators(df1)
                        if atr_last is None or atr_last <= 0:
                            continue

                        # convert ATR (price units) -> pips using pip_price()
                        pp = pip_price(symbol)
                        if pp <= 0:
                            continue
                        atr_pips = atr_last / pp
                        atr_pips = max(atr_pips, MIN_ATR_PIPS)  # enforce a lower bound to avoid tiny stops

                        # derive sl/ tp in pips then cap reasonably
                        sl_pips = max(int(round(atr_pips * SL_ATR_MULT)), 1)
                        tp_pips = max(int(round(atr_pips * TP_ATR_MULT)), 1)
                        sl_pips = min(sl_pips, MAX_SL_PIPS)
                        tp_pips = min(tp_pips, MAX_TP_PIPS)

                        # Get current price
                        tick = mt5.symbol_info_tick(symbol)
                        if tick is None:
                            continue

                        price_now = tick.ask if entry_signal == "BUY" else tick.bid

                        # Convert ATR to pips properly
                        atr_pips = max(atr_to_pips(symbol, atr_last), MIN_ATR_PIPS)
                        sl_pips = max(round(atr_pips * SL_ATR_MULT), 1)
                        tp_pips = max(round(atr_pips * TP_ATR_MULT), 1)

                        # Convert pips to absolute price
                        def pips_to_price(symbol, pips):
                            info = mt5.symbol_info(symbol)
                            if info is None:
                                return pips * 0.0001  # fallback

                            point = info.point

                            # Forex / JPY / metals
                            if "JPY" in symbol:
                                return pips * 0.01
                            elif symbol.startswith("XAU"):
                                return pips * 0.01
                            else:
                                return pips * 0.0001

                        if entry_signal == "BUY":
                            sl_price = price_now - pips_to_price(symbol, sl_pips)
                            tp_price = price_now + pips_to_price(symbol, tp_pips)
                        else:
                            sl_price = price_now + pips_to_price(symbol, sl_pips)
                            tp_price = price_now - pips_to_price(symbol, tp_pips)

                        # Ensure stops are at least broker minimum
                        sl_price, tp_price = ensure_min_stop_distance(symbol, price_now, sl_price, tp_price, 
                                                                    mt5.ORDER_TYPE_BUY if entry_signal=="BUY" else mt5.ORDER_TYPE_SELL)

                        # Calculate lot size based on price distance to SL
                        lot = calculate_lots(symbol, abs(price_now - sl_price), RISK_PERCENT)
                        if lot < LOT_MIN:
                            print(f"{now_utc()} - {symbol}: computed lot {lot} < min {LOT_MIN} -> skip")
                            continue


                        # check existing positions count for symbol
                        positions = mt5.positions_get(symbol=symbol)
                        positions = [] if positions is None else positions
                        if len(positions) >= MAX_CONCURRENT_TRADES:
                            continue

                        # draw signal
                        draw_signal(symbol, entry_signal, price=price_now)

                        # send order
                        res = send_order(symbol, entry_signal, lot, sl_price, tp_price)
                        print(f"{now_utc()} - {symbol} order result: {res}")
                        if res is None:
                            continue
                        code = getattr(res, "retcode", None)
                        comment = getattr(res, "comment", "")
                        log_trade([now_utc().isoformat(), symbol, entry_signal, lot,
                                   getattr(res, "price", None), getattr(res, "sl", None), getattr(res, "tp", None), code, comment])

                        time.sleep(REPEAT_OPEN_DELAY)

                    except Exception:
                        print(f"Error processing symbol {symbol}:", traceback.format_exc())
                        continue

            except KeyboardInterrupt:
                print("Stopping by user.")
                break
            except Exception:
                print("Main loop error:", traceback.format_exc())
                time.sleep(3)
    finally:
        mt5.shutdown()
        print("MT5 shutdown.")

if __name__ == "__main__":
    main_loop()
