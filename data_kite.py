"""
Zerodha Kite as a data source, in place of Yahoo.

    pip install kiteconnect
    set KITE_API_KEY=...
    set KITE_ACCESS_TOKEN=...        # expires daily, see below
    # then in orb_backtest.py: DATA_PROVIDER = "kite"

Why bother: Yahoo's NSE intraday bars carry bad ticks, and a spurious high or
low fabricates a stop-out that never happened. Every run so far has been
dominated by stops, so some of them may be artifacts rather than price action.
Kite bars are built from the exchange feed. Kite also lifts the 60-day intraday
ceiling, which is what caps every result here at ~41 sessions.

Getting an access token (it expires every morning):
    kite = KiteConnect(api_key=KEY)
    print(kite.login_url())              # open, log in, copy request_token
    data = kite.generate_session(request_token, api_secret=SECRET)
    data["access_token"]

Historical data is a paid Kite Connect add-on and needs an active subscription.

UNVERIFIED: this module has not been run against the live API. It was written
without network access to Kite, so treat the request-range caps and the exact
response field names as the first things to check. The failure mode is loud
(KeyError or an API error), not silent.
"""

import datetime
import os
import time

import pandas as pd

IST = "Asia/Kolkata"
INSTRUMENTS_CACHE = "kite_instruments.csv"

# Kite caps how much history one historical_data call may span, and the cap
# depends on the interval. These are deliberately conservative; widen them once
# you have confirmed the current limits against the docs.
MAX_DAYS_PER_CALL = {
    "minute": 60, "3minute": 90, "5minute": 90, "10minute": 90,
    "15minute": 180, "30minute": 180, "60minute": 365, "day": 2000,
}

# Historical endpoint is rate limited (a few requests a second). Sleeping
# between calls is far cheaper than getting throttled mid-fetch.
REQUEST_GAP_S = 0.35

_kite = None
_tokens = None


def kite():
    """Authenticated client from KITE_API_KEY / KITE_ACCESS_TOKEN."""
    global _kite
    if _kite is not None:
        return _kite
    try:
        from kiteconnect import KiteConnect
    except ImportError:
        raise SystemExit("pip install kiteconnect")
    key, tok = os.getenv("KITE_API_KEY"), os.getenv("KITE_ACCESS_TOKEN")
    if not key or not tok:
        raise SystemExit("set KITE_API_KEY and KITE_ACCESS_TOKEN "
                         "(the access token expires daily)")
    _kite = KiteConnect(api_key=key)
    _kite.set_access_token(tok)
    return _kite


def instruments(exchange="NSE", refresh=False):
    """tradingsymbol -> instrument_token. Cached; the dump is large and static
    within a day."""
    global _tokens
    if _tokens is not None and not refresh:
        return _tokens
    if os.path.exists(INSTRUMENTS_CACHE) and not refresh:
        age = time.time() - os.path.getmtime(INSTRUMENTS_CACHE)
        if age < 86400:
            df = pd.read_csv(INSTRUMENTS_CACHE)
            _tokens = dict(zip(df.tradingsymbol, df.instrument_token))
            return _tokens
    df = pd.DataFrame(kite().instruments(exchange))
    if "segment" in df.columns:            # equities only, no F&O series
        df = df[df.segment == f"{exchange}"]
    df[["tradingsymbol", "instrument_token"]].to_csv(INSTRUMENTS_CACHE, index=False)
    _tokens = dict(zip(df.tradingsymbol, df.instrument_token))
    return _tokens


def _bare(symbol):
    """RELIANCE.NS -> RELIANCE. The rest of the repo speaks Yahoo suffixes."""
    return symbol.split(".")[0].upper()


def _candles(token, start, end, interval):
    """One symbol, one interval, chunked to stay inside the per-call range cap."""
    cap = MAX_DAYS_PER_CALL.get(interval, 60)
    rows, cur = [], start
    while cur <= end:
        stop = min(cur + datetime.timedelta(days=cap - 1), end)
        try:
            rows += kite().historical_data(token, cur, stop, interval)
        except Exception as e:                      # one bad window, not the run
            print(f"  kite historical_data {cur}..{stop} failed: {e}")
        time.sleep(REQUEST_GAP_S)
        cur = stop + datetime.timedelta(days=1)
    return rows


def _frame(rows):
    """Kite candles -> the OHLCV frame shape the rest of the repo expects."""
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).rename(columns={
        "open": "Open", "high": "High", "low": "Low",
        "close": "Close", "volume": "Volume"})
    df["date"] = pd.to_datetime(df["date"])
    if df["date"].dt.tz is None:
        df["date"] = df["date"].dt.tz_localize(IST)
    else:
        df["date"] = df["date"].dt.tz_convert(IST)
    return df.sort_values("date").reset_index(drop=True)


def fetch_daily(universe=None, days=None):
    """Drop-in for symbol_screener.fetch_daily. {symbol: df indexed by date}."""
    from symbol_screener import load_universe, LOOKBACK_DAYS
    universe = universe or load_universe()
    days = days or LOOKBACK_DAYS + 20
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(days))
    toks = instruments()

    out, missing = {}, []
    for i, sym in enumerate(universe, 1):
        tok = toks.get(_bare(sym))
        if not tok:
            missing.append(sym)
            continue
        df = _frame(_candles(tok, start, end, "day"))
        if not df.empty:
            out[sym] = df.set_index("date")[["Open","High","Low","Close","Volume"]]
        if i % 100 == 0:
            print(f"  daily {i}/{len(universe)}")
    if missing:
        print(f"no Kite instrument for {len(missing)} symbols"
              f"{': ' + ', '.join(missing[:6]) if len(missing) <= 6 else ''}")
    if not out:
        raise SystemExit("Kite returned no daily data. Check the token and subscription.")
    print(f"pool {len(universe)} symbols -> {len(out)} with usable history")
    return out


def load_intraday(symbols, interval="15m", period="60d"):
    """Drop-in for orb_backtest.load_many. {symbol: normalized intraday df}."""
    kite_interval = {"1m":"minute","3m":"3minute","5m":"5minute","10m":"10minute",
                     "15m":"15minute","30m":"30minute","60m":"60minute"}[interval]
    end = datetime.date.today()
    start = end - datetime.timedelta(days=int(str(period).rstrip("d")))
    toks = instruments()

    out = {}
    for i, sym in enumerate(symbols, 1):
        tok = toks.get(_bare(sym))
        if not tok:
            continue
        df = _frame(_candles(tok, start, end, kite_interval))
        if df.empty:
            continue
        # same helper columns _normalize() builds on the Yahoo path
        df["dt"] = df["date"]
        df["time"] = df["date"].dt.strftime("%H:%M")
        df["date"] = df["date"].dt.date
        out[sym] = df
        if i % 25 == 0:
            print(f"  intraday {i}/{len(symbols)}")
    return out


if __name__ == "__main__":
    # Smoke test: one liquid name, both intervals, before trusting a full run.
    toks = instruments()
    print(f"{len(toks):,} NSE instruments cached -> {INSTRUMENTS_CACHE}")
    d = fetch_daily(["RELIANCE.NS"], days=30)
    i = load_intraday(["RELIANCE.NS"], "15m", "5d")
    for name, frames in (("daily", d), ("intraday", i)):
        for sym, df in frames.items():
            print(f"\n{name} {sym}: {len(df)} bars")
            print(df.tail(3).to_string())
