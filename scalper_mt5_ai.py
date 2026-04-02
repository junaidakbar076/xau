import os
import time
import math
import traceback
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from ai_helper import GoldAI  # Make sure ai_helper.py is updated!

# Technical Analysis (for Trend/ATR)
from ta.trend import EMAIndicator
from ta.volatility import AverageTrueRange
import tkinter as tk
from tkinter import ttk
from threading import Thread

load_dotenv()

load_dotenv()

# --- Dashboard Class ---
class TradingDashboard:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Gold AI Monitor")
        self.root.geometry("760x420")
        self.root.attributes("-topmost", True) # Keep window on top

        self.price_label = tk.Label(self.root, text="Price: --", font=("Arial", 12))
        self.price_label.pack(pady=2)

        self.pred_label = tk.Label(self.root, text="AI Predict: --", font=("Arial", 12), fg="blue")
        self.pred_label.pack(pady=2)

        self.signal_label = tk.Label(self.root, text="SIGNAL: WAIT", font=("Arial", 14, "bold"))
        self.signal_label.pack(pady=2)

        self.trend_label = tk.Label(self.root, text="M5 Trend: --", font=("Arial", 10))
        self.trend_label.pack(pady=2)

        # Trade log frame
        frame = tk.Frame(self.root)
        frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.tree = ttk.Treeview(frame, columns=("Symbol", "Side", "Volume", "Entry", "Status", "P/L", "OpenTime", "CloseTime"), show="headings")
        for col in ("Symbol", "Side", "Volume", "Entry", "Status", "P/L", "OpenTime", "CloseTime"):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=90, anchor=tk.CENTER)

        self.vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=self.vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.trade_rows = {}

    def insert_trade(self, trade_id, symbol, side, volume, entry_price, open_time):
        row_id = self.tree.insert("", tk.END, values=(symbol, side, f"{volume:.2f}", f"{entry_price:.2f}", "OPEN", "0.00", open_time.strftime('%H:%M:%S'), ""))
        self.trade_rows[trade_id] = row_id

    def update_trade(self, trade_id, status, profit, close_time):
        row_id = self.trade_rows.get(trade_id)
        if not row_id:
            return
        values = list(self.tree.item(row_id, "values"))
        values[4] = status
        values[5] = f"{profit:.2f}"
        values[7] = close_time.strftime('%H:%M:%S') if close_time else values[7]
        self.tree.item(row_id, values=values)
        if status == "CLOSED":
            self.tree.item(row_id, tags=("closed",))
            self.tree.tag_configure("closed", background="#f0f0f0")

    def update(self, price, pred, signal, trend):
        self.price_label.config(text=f"Current: ${price:.2f}")
        self.pred_label.config(text=f"AI Target: ${pred:.2f}")
        self.trend_label.config(text=f"M5 Trend: {trend}")
        
        if signal == "BUY":
            self.signal_label.config(text="SIGNAL: BUY", fg="green")
        elif signal == "SELL":
            self.signal_label.config(text="SIGNAL: SELL", fg="red")
        else:
            self.signal_label.config(text="SIGNAL: WAIT", fg="gray")

    def update(self, price, pred, signal, trend):
        self.price_label.config(text=f"Current: ${price:.2f}")
        self.pred_label.config(text=f"AI Target: ${pred:.2f}")
        self.trend_label.config(text=f"M5 Trend: {trend}")
        
        if signal == "BUY":
            self.signal_label.config(text="SIGNAL: BUY", fg="green")
        elif signal == "SELL":
            self.signal_label.config(text="SIGNAL: SELL", fg="red")
        else:
            self.signal_label.config(text="SIGNAL: WAIT", fg="gray")

# --- Initialize ---
db = TradingDashboard()

# --- CONFIG (Matched to your .env) ---
MAJOR_PAIRS = ["XAUUSD"]
RISK_PERCENT = float(os.getenv("RISK_PERCENT", "0.2"))
LOT_MIN = float(os.getenv("LOT_MIN", "0.01"))
HARD_MAX_LOT = float(os.getenv("HARD_MAX_LOT", "0.05"))
MAX_SL_PIPS = int(os.getenv("MAX_SL_PIPS", "60"))  # tighter SL cap
MAX_TP_PIPS = int(os.getenv("MAX_TP_PIPS", "35"))  # smaller TP cap
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "1.2"))  # smaller ATR SL buffer
TP_ATR_MULT = float(os.getenv("TP_ATR_MULT", "0.8"))  # target now < ATR to avoid oversized TP
TRADE_RR = float(os.getenv("TRADE_RR", "0.85"))  # risk-reward ratio (TP = SL * RR)
MIN_ATR_PIPS = 8.0
MAGIC = int(os.getenv("MAGIC", "987654"))

# Initialize AI
ai_engine = GoldAI()

def now_utc():
    return datetime.now(timezone.utc)

def fetch_ohlcv(symbol, timeframe, n=100):
    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, n)
    if rates is None or len(rates) == 0: return None
    return pd.DataFrame(rates)

def m5_trend_direction(symbol):
    df5 = fetch_ohlcv(symbol, mt5.TIMEFRAME_M5, n=210)
    if df5 is None or len(df5) < 200: return None
    ema50 = EMAIndicator(df5['close'], window=50).ema_indicator().iloc[-1]
    ema200 = EMAIndicator(df5['close'], window=200).ema_indicator().iloc[-1]
    return "BUY" if ema50 > ema200 else "SELL"

# --- Lot & Price Helpers ---
def pips_to_price(symbol, pips):
    return pips * 0.01 if "XAU" in symbol or "JPY" in symbol else pips * 0.0001

def calculate_lots(symbol, sl_pips, risk_percent):
    account = mt5.account_info()
    if not account: return LOT_MIN
    risk_amount = account.equity * (risk_percent / 100.0)
    # Approx pip value for Gold is $1 per 0.01 move for 1 lot (check broker)
    raw_lot = risk_amount / (max(sl_pips, 1.0) * 1.0) 
    info = mt5.symbol_info(symbol)
    step = info.volume_step if info else 0.01
    lot = math.floor(raw_lot / step) * step
    return min(max(lot, LOT_MIN), HARD_MAX_LOT)

trade_store = {}

def check_closed_trades():
    for trade_key, trade in list(trade_store.items()):
        if trade.get("status") != "OPEN":
            continue
            
        position_ticket = trade.get("position_ticket")
        if not position_ticket:
            continue

        # 1. Check if the position is still "Live"
        # If positions_get returns nothing for this ticket, it's closed.
        active_pos = mt5.positions_get(ticket=position_ticket)
        
        if not active_pos or len(active_pos) == 0:
            # 2. It's closed! Now fetch the history ONLY for this Position ID
            # We use a very wide time range because filtering by position_id is instant
            from_date = datetime.now(timezone.utc) - timedelta(days=2)
            to_date = datetime.now(timezone.utc) + timedelta(minutes=5)
            
            history_deals = mt5.history_deals_get(from_date, to_date, position=position_ticket)
            
            if history_deals:
                # Calculate net profit (Profit + Swap + Commission)
                net_profit = sum(d.profit + d.commission + d.swap for d in history_deals)
                
                # Update Store
                trade_store[trade_key]['status'] = 'CLOSED'
                trade_store[trade_key]['profit'] = net_profit
                trade_store[trade_key]['close_time'] = datetime.now(timezone.utc)
                
                # Update Dashboard UI
                db.update_trade(trade_key, 'CLOSED', net_profit, datetime.now(timezone.utc))
                print(f"✅ Trade {position_ticket} closed via ID. Net P/L: {net_profit:.2f}")

def run_bot():
    if not mt5.initialize(): 
        print("MT5 Initialization failed")
        return
    
    while True:
        try:
            symbol = "XAUUSD"
            # update closed trades from MT5 history before any new signal
            check_closed_trades()
            # 1. Fetch Data
            tick = mt5.symbol_info_tick(symbol)
            df1 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 70)
            df5 = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M5, 0, 210)
            print(f"Data fetched at {datetime.now().strftime('%H:%M:%S')}. M1 candles: {len(df1)}, M5 candles: {len(df5)}")
            if tick and df1 is not None and df5 is not None:
                df1_pd = pd.DataFrame(df1)
                df5_pd = pd.DataFrame(df5)
                
                # 2. Get AI Prediction (Note the variable name: ai_signal)
                ai_signal, raw_pred = ai_engine.get_prediction_with_raw(df1_pd)
                
                # 3. Get Trend
                ema50 = EMAIndicator(df5_pd['close'], window=50).ema_indicator().iloc[-1]
                ema200 = EMAIndicator(df5_pd['close'], window=200).ema_indicator().iloc[-1]
                trend_dir = "BUY" if ema50 > ema200 else "SELL"
                
                # 4. Update Dashboard
                db.update(tick.bid, raw_pred, ai_signal, trend_dir)
                
                # 5. Trading Logic (The Trigger)
                # Now 'ai_signal' is correctly defined for this check
                if ai_signal and ai_signal == trend_dir:
                    # Check if we already have a position to avoid spamming
                    positions = mt5.positions_get(symbol=symbol)
                    if positions and len(positions) > 3: # If this list is more than 3 then skip (some brokers have multiple entries for the same symbol)
                        print(f"Signal active, but {symbol} position already exists. Skipping.")
                        time.sleep(5) # Wait before next check
                        continue
                    
                    if not positions:
                        print(f"MATCH! AI: {ai_signal} | Trend: {trend_dir}. Executing...")

                    # 3. Refined Calculation Logic
                    tick = mt5.symbol_info_tick(symbol)
                    price_now = tick.ask if ai_signal == "BUY" else tick.bid
                    
                    # Use a slightly longer ATR for stability
                    atr_indicator = AverageTrueRange(pd.Series(df1_pd['high']), pd.Series(df1_pd['low']), pd.Series(df1_pd['close']), window=20)
                    atr = atr_indicator.average_true_range().iloc[-1]
                    atr_pips = max(atr / 0.01, 15.0) # Gold minimum 15 pip volatility

                    # CALCULATE SL: Based on volatility
                    sl_pips = min(round(atr_pips * SL_ATR_MULT), MAX_SL_PIPS)
                    # CALCULATE TP: Based on a fixed 1.5x Risk-Reward ratio
                    # This ensures that one win covers 1.5 losses
                    tp_pips = round(sl_pips * 1.5) 
                    # Avoid a tiny TP that is dead in the water
                    if tp_pips < 4:
                        tp_pips = min(4, MAX_TP_PIPS)
                    # Safety Cap
                    if tp_pips > MAX_TP_PIPS: tp_pips = MAX_TP_PIPS
                    if tp_pips < 15: tp_pips = 15 # Avoid "Micro-trading" spread death
                    sl_price = price_now - pips_to_price(symbol, sl_pips) if ai_signal == "BUY" else price_now + pips_to_price(symbol, sl_pips)
                    tp_price = price_now + pips_to_price(symbol, tp_pips) if ai_signal == "BUY" else price_now - pips_to_price(symbol, tp_pips)
                    
                    lot = calculate_lots(symbol, sl_pips, RISK_PERCENT)

                    # 4. Send Order
                    request = {
                        "action": mt5.TRADE_ACTION_DEAL,
                        "symbol": symbol,
                        "volume": lot,
                        "type": mt5.ORDER_TYPE_BUY if ai_signal == "BUY" else mt5.ORDER_TYPE_SELL,
                        "price": price_now,
                        "sl": sl_price,
                        "tp": tp_price,
                        "magic": MAGIC,
                        "comment": "AI_SCALPER",
                        "type_filling": mt5.ORDER_FILLING_FOK,
                    }
                    res = mt5.order_send(request)
                    if res and res.retcode == mt5.TRADE_RETCODE_DONE:
                        # identify position ticket if available
                        position_ticket = None
                        positions_post = mt5.positions_get(symbol=symbol) or []
                        for pos in positions_post:
                            if abs(pos.volume - lot) < 1e-8 and ((ai_signal == "BUY" and pos.type == mt5.POSITION_TYPE_BUY) or (ai_signal == "SELL" and pos.type == mt5.POSITION_TYPE_SELL)):
                                position_ticket = pos.ticket
                                break

                        trade_key = res.order if hasattr(res, 'order') and res.order else (res.deal if hasattr(res, 'deal') else str(time.time()))
                        trade_store[trade_key] = {
                            'symbol': symbol,
                            'side': ai_signal,
                            'volume': lot,
                            'entry': price_now,
                            'open_time': datetime.now(timezone.utc),
                            'status': 'OPEN',
                            'position_ticket': position_ticket,
                            'profit': 0.0,
                        }
                        db.insert_trade(trade_key, symbol, ai_signal, lot, price_now, datetime.now(timezone.utc))

                    print(f"Trade Opened: {ai_signal} {lot} lots. Result: {res.retcode if res else 'Fail'}")

            time.sleep(15) # Check every 15 seconds
        except Exception:
            print(traceback.format_exc())
            time.sleep(5)
if __name__ == "__main__":
    # Start the trading logic in the background
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Start the Dashboard window in the foreground (this "holds" the program open)
    print("Dashboard starting...")
    db.root.mainloop()