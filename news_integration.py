
"""
news_integration.py
Provides functions to check live economic calendar events relevant to XAUUSD (gold).
Primary approach:
 - Use MyFxBook API (user provides credentials via environment variables)
 - If MyFxBook is unavailable, try MT5's calendar (if available)
 - Filter events by currency (USD, CNY, EUR) and by impact (High/Medium)
 - Expose: init_news_session(), is_gold_news_now(window_minutes=45)

NOTE: This module performs live HTTP requests when MyFxBook is configured.
"""

import os
from datetime import datetime, timedelta
import requests
import time

# MyFxBook config via environment
MYFXBOOK_EMAIL = os.getenv("MYFXBOOK_EMAIL", "")
MYFXBOOK_PASSWORD = os.getenv("MYFXBOOK_PASSWORD", "")
MYFXBOOK_SESSION = None

# currencies that influence gold
GOLD_CURRENCIES = {"USD", "CNY", "EUR", "GBP"}

def init_news_session():
    """Attempt to login to MyFxBook and store a session token in module state."""
    global MYFXBOOK_SESSION
    if not MYFXBOOK_EMAIL or not MYFXBOOK_PASSWORD:
        # credentials not provided -> skip
        return None
    try:
        url = "https://www.myfxbook.com/api/login.json"
        params = {"email": MYFXBOOK_EMAIL, "password": MYFXBOOK_PASSWORD}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("error"):
            print("MyFxBook login error:", data.get("message"))
            return None
        MYFXBOOK_SESSION = data.get("session")
        print("MyFxBook session established.")
        return MYFXBOOK_SESSION
    except Exception as e:
        print("MyFxBook login failed:", e)
        return None

def get_myfxbook_calendar():
    """Fetch economic calendar from MyFxBook (requires login)."""
    global MYFXBOOK_SESSION
    if not MYFXBOOK_SESSION:
        init_news_session()
    if not MYFXBOOK_SESSION:
        return []
    try:
        url = f"https://www.myfxbook.com/api/get-economic-calendar.json?session={MYFXBOOK_SESSION}"
        r = requests.get(url, timeout=10)
        data = r.json()
        if data.get("error"):
            print("Error retrieving calendar:", data.get("message"))
            return []
        return data.get("calendar", [])
    except Exception as e:
        print("Failed to fetch MyFxBook calendar:", e)
        return []

def filter_gold_events(calendar, window_minutes=45):
    """Return events relevant to gold within +/- window_minutes of now."""
    now = datetime.utcnow()
    relevant = []
    for ev in calendar:
        try:
            impact = ev.get("impact", "")
            if impact not in ("High", "Medium"):
                continue
            currency = ev.get("currency", "")
            if currency not in GOLD_CURRENCIES:
                continue
            # event time parsing: MyFxBook timestamp like "2025-11-24 13:30:00"
            ts = ev.get("timestamp") or ev.get("utcDate") or ev.get("date")
            if not ts:
                continue
            # try multiple formats
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
                try:
                    event_time = datetime.strptime(ts, fmt)
                    break
                except:
                    event_time = None
            if event_time is None:
                continue
            delta_min = abs((event_time - now).total_seconds()) / 60.0
            if delta_min <= window_minutes:
                relevant.append({
                    "time": event_time,
                    "currency": currency,
                    "impact": impact,
                    "title": ev.get("title") or ev.get("event") or ev.get("description", "")
                })
        except Exception:
            continue
    return relevant

def is_gold_news_now(window_minutes=45):
    """Return True if a high/medium impact gold-relevant event is within window_minutes of now."""
    # Try MyFxBook first
    calendar = get_myfxbook_calendar()
    events = filter_gold_events(calendar, window_minutes=window_minutes)
    if events:
        print("MyFxBook events found:", events)
        return True
    # Fall back to MT5 calendar if available
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            return False
        cal = mt5.calendar_event_get()
        now = datetime.utcnow()
        for ev in cal:
            try:
                imp = getattr(ev, "importance", 0)
                # impact: 2 (medium), 3 (high)
                if imp < 2:
                    continue
                currency = getattr(ev, "currency", "")
                if currency not in GOLD_CURRENCIES:
                    continue
                t = getattr(ev, "time", None)
                if not isinstance(t, datetime):
                    continue
                if abs((t - now).total_seconds()) / 60.0 <= window_minutes:
                    print("MT5 calendar event blocks trading:", ev)
                    return True
            except Exception:
                continue
    except Exception:
        # MT5 not available - ignore
        pass
    return False
