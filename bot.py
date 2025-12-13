
import MetaTrader5 as mt5
from datetime import datetime
import time

MAX_SPREAD_MULTIPLIER = 3
median_spread = 30  # starting point

def update_median_spread(symbol):
    global median_spread
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        return
    spread = (tick.ask - tick.bid) / info.point
    median_spread = (median_spread * 0.9) + (spread * 0.1)

def is_spread_ok(symbol):
    info = mt5.symbol_info(symbol)
    tick = mt5.symbol_info_tick(symbol)
    if not info or not tick:
        return False
    spread = (tick.ask - tick.bid) / info.point
    if spread > median_spread * MAX_SPREAD_MULTIPLIER:
        print(f"Spread too high: {spread} > allowed {median_spread * MAX_SPREAD_MULTIPLIER}")
        return False
    return True
